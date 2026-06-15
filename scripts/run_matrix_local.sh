#!/usr/bin/env bash
# =============================================================================
# Local Test Matrix Runner
# =============================================================================
# Runs integration tests across all transport × Couchbase version × server
# binary combinations.
#
# Prerequisites:
#   - Docker installed and running
#   - uv installed
#   - Project dependencies installed (uv sync --extra dev)
#
# Usage:
#   ./scripts/run_matrix_local.sh
#
# Optional: run a subset for debugging:
#   CB_VERSIONS="8.0.0" TRANSPORTS="stdio" SERVER_BINARIES="source" ./scripts/run_matrix_local.sh
#
# Server binaries:
#   source  - run directly from source via uv
#   docker  - build Docker image and run as a container (HTTP only)
#   pypi    - build a wheel, install it, and run the installed binary (HTTP only)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (override via env if you want a subset)
# ---------------------------------------------------------------------------
CB_VERSIONS="${CB_VERSIONS:-8.0.0 7.6.11}"
TRANSPORTS="${TRANSPORTS:-stdio http}"
SERVER_BINARIES="${SERVER_BINARIES:-source docker pypi}"
CB_USERNAME="Administrator"
CB_PASSWORD="password"
CB_MCP_TEST_BUCKET="travel-sample"
CONTAINER_NAME="cb-mcp-test"
MCP_CONTAINER_NAME="mcp-server-http-test"
DOCKER_NETWORK="cb-mcp-test-net"
PYPI_VENV="/tmp/cb-mcp-pypi-venv"
HOST_PORT=8091
KV_PORT=11210
MCP_PORT=8000

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# ---------------------------------------------------------------------------
# Cleanup on exit (success, failure, or Ctrl+C)
# ---------------------------------------------------------------------------
declare -a BACKGROUND_PIDS=()

cleanup() {
    local exit_code=$?
    for pid in "${BACKGROUND_PIDS[@]:-}"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker rm -f "$MCP_CONTAINER_NAME" 2>/dev/null || true
    docker network rm "$DOCKER_NETWORK" 2>/dev/null || true
    rm -rf "$PYPI_VENV"
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
declare -a RESULTS=()

record_result() {
    local version="$1" transport="$2" binary="$3" exit_code="$4"
    if [[ "$exit_code" -eq 0 ]]; then
        RESULTS+=("PASS|$version|$transport|$binary")
    else
        RESULTS+=("FAIL|$version|$transport|$binary")
    fi
}

# ---------------------------------------------------------------------------
# Cluster lifecycle helpers
# ---------------------------------------------------------------------------
stop_container() {
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
}

start_couchbase() {
    local version="$1"
    echo ""
    echo "============================================"
    echo " Starting Couchbase Server $version"
    echo "============================================"

    stop_container
    docker network create "$DOCKER_NETWORK" 2>/dev/null || true

    docker run -d --name "$CONTAINER_NAME" \
        --network "$DOCKER_NETWORK" \
        -p 8091-8096:8091-8096 \
        -p 9102:9102 \
        -p 11210:11210 \
        -p 11207:11207 \
        "couchbase:enterprise-${version}"

    echo "Waiting for Couchbase REST API..."
    for i in $(seq 1 60); do
        if curl -s http://localhost:$HOST_PORT/pools > /dev/null 2>&1; then
            echo "  REST API responding (attempt $i)"
            break
        fi
        sleep 2
    done
}

initialize_cluster() {
    echo "Initializing cluster..."

    curl -s -X POST http://localhost:$HOST_PORT/nodes/self/controller/settings \
        -d 'path=/opt/couchbase/var/lib/couchbase/data' \
        -d 'index_path=/opt/couchbase/var/lib/couchbase/data'

    curl -s -X POST http://localhost:$HOST_PORT/node/controller/setupServices \
        -d 'services=kv,n1ql,index,fts'

    curl -s -X POST http://localhost:$HOST_PORT/pools/default \
        -d 'memoryQuota=512' \
        -d 'indexMemoryQuota=256' \
        -d 'ftsMemoryQuota=256'

    curl -s -X POST http://localhost:$HOST_PORT/settings/web \
        -d "password=$CB_PASSWORD" \
        -d "username=$CB_USERNAME" \
        -d 'port=SAME'

    curl -s -X PUT \
        -u "$CB_USERNAME:$CB_PASSWORD" \
        "http://localhost:$HOST_PORT/node/controller/setupAlternateAddresses/external" \
        -d "hostname=127.0.0.1"

    echo "  Cluster initialized."
}

load_sample_bucket() {
    echo "Loading travel-sample bucket..."
    sleep 5

    curl -s -X POST http://localhost:$HOST_PORT/sampleBuckets/install \
        -u "$CB_USERNAME:$CB_PASSWORD" \
        -H "Content-Type: application/json" \
        -d '["travel-sample"]'

    echo "  Waiting for bucket to be healthy..."
    for i in $(seq 1 60); do
        if curl -s -u "$CB_USERNAME:$CB_PASSWORD" \
            http://localhost:$HOST_PORT/pools/default/buckets/travel-sample 2>/dev/null \
            | grep -q '"status":"healthy"'; then
            echo "  Bucket healthy (attempt $i)"
            break
        fi
        sleep 3
    done

    echo "  Waiting for KV port ($KV_PORT) to be ready..."
    for i in $(seq 1 30); do
        if nc -z 127.0.0.1 "$KV_PORT" 2>/dev/null; then
            echo "  KV port responding (attempt $i)"
            break
        fi
        sleep 2
    done

    echo "  Waiting for bucket data to load..."
    for i in $(seq 1 60); do
        item_count=$(curl -s -u "$CB_USERNAME:$CB_PASSWORD" \
            http://localhost:$HOST_PORT/pools/default/buckets/travel-sample \
            | python3 -c "import sys,json; print(json.load(sys.stdin).get('basicStats',{}).get('itemCount',0))" 2>/dev/null || echo 0)
        if [[ "$item_count" -gt 0 ]]; then
            echo "  Bucket has $item_count items (attempt $i)"
            break
        fi
        sleep 3
    done
    sleep 5
}

set_indexer_mode() {
    echo "Setting indexer storage mode..."
    sleep 5
    curl -s -X POST http://localhost:$HOST_PORT/settings/indexes \
        -u "$CB_USERNAME:$CB_PASSWORD" \
        -d 'storageMode=plasma' || true
    sleep 5
}

setup_test_data() {
    echo "Running setup_test_data.py..."
    for attempt in $(seq 1 5); do
        if CB_CONNECTION_STRING="couchbase://127.0.0.1" \
           CB_USERNAME="$CB_USERNAME" \
           CB_PASSWORD="$CB_PASSWORD" \
           CB_MCP_TEST_BUCKET="$CB_MCP_TEST_BUCKET" \
           PYTHONPATH=src \
               uv run python scripts/setup_test_data.py; then
            echo "  setup_test_data.py succeeded."
            return 0
        fi
        echo "  setup_test_data.py failed (attempt $attempt/5), retrying in 15s..."
        sleep 15
    done
    echo "  ERROR: setup_test_data.py failed after 5 attempts."
    return 1
}

# ---------------------------------------------------------------------------
# Binary build helpers (called once before the main loop)
# ---------------------------------------------------------------------------
build_docker_image() {
    echo ""
    echo "============================================"
    echo " Building Docker image"
    echo "============================================"
    docker build -t mcp-server-test:latest .
    echo "  Docker image built."
}

build_pypi_wheel() {
    echo ""
    echo "============================================"
    echo " Building PyPI wheel"
    echo "============================================"
    uv build
    rm -rf "$PYPI_VENV"
    python3 -m venv "$PYPI_VENV"
    "$PYPI_VENV/bin/pip" install --quiet dist/couchbase_mcp_server-*.whl
    echo "  Wheel installed in $PYPI_VENV"
}

# ---------------------------------------------------------------------------
# Test runners per transport × binary
# ---------------------------------------------------------------------------
run_stdio_tests() {
    echo "  [stdio/source] Running tests..."
    CB_CONNECTION_STRING="couchbase://127.0.0.1" \
    CB_USERNAME="$CB_USERNAME" \
    CB_PASSWORD="$CB_PASSWORD" \
    CB_MCP_TRANSPORT="stdio" \
    CB_MCP_TEST_BUCKET="$CB_MCP_TEST_BUCKET" \
    CB_MCP_TEST_SCOPE="inventory" \
    CB_MCP_TEST_COLLECTION="airline" \
    PYTHONPATH=src \
        uv run pytest tests/integration -v --tb=short
}

run_http_source_tests() {
    echo "  [http/source] Starting MCP server..."
    CB_CONNECTION_STRING="couchbase://127.0.0.1" \
    CB_USERNAME="$CB_USERNAME" \
    CB_PASSWORD="$CB_PASSWORD" \
    CB_MCP_TRANSPORT="http" \
    CB_MCP_HOST="127.0.0.1" \
    CB_MCP_PORT="$MCP_PORT" \
    CB_MCP_READ_ONLY_MODE="false" \
    PYTHONPATH=src \
        uv run python -m mcp_server &
    local server_pid=$!
    BACKGROUND_PIDS+=("$server_pid")

    for i in $(seq 1 30); do
        if nc -z 127.0.0.1 "$MCP_PORT" 2>/dev/null; then break; fi
        sleep 1
    done

    echo "  [http/source] Running tests..."
    local ret=0
    CB_CONNECTION_STRING="couchbase://127.0.0.1" \
    CB_USERNAME="$CB_USERNAME" \
    CB_PASSWORD="$CB_PASSWORD" \
    CB_MCP_TRANSPORT="http" \
    MCP_SERVER_URL="http://127.0.0.1:${MCP_PORT}/mcp" \
    CB_MCP_TEST_BUCKET="$CB_MCP_TEST_BUCKET" \
    CB_MCP_TEST_SCOPE="inventory" \
    CB_MCP_TEST_COLLECTION="airline" \
    PYTHONPATH=src \
        uv run pytest tests/integration -v --tb=short || ret=$?

    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    return $ret
}

run_http_docker_tests() {
    echo "  [http/docker] Starting MCP server container..."
    docker rm -f "$MCP_CONTAINER_NAME" 2>/dev/null || true
    # Use the shared Docker network so the MCP container reaches Couchbase by
    # container name. This avoids the alternate-address trap: bootstrapping via
    # the container name doesn't match the external alternate address (127.0.0.1)
    # so the SDK stays in internal-address mode and uses the Docker network IPs.
    docker run -d --name "$MCP_CONTAINER_NAME" \
        --network "$DOCKER_NETWORK" \
        -p "${MCP_PORT}:${MCP_PORT}" \
        -e CB_CONNECTION_STRING="couchbase://$CONTAINER_NAME" \
        -e CB_USERNAME="$CB_USERNAME" \
        -e CB_PASSWORD="$CB_PASSWORD" \
        -e CB_MCP_TRANSPORT="http" \
        -e CB_MCP_HOST="0.0.0.0" \
        -e CB_MCP_PORT="$MCP_PORT" \
        -e CB_MCP_READ_ONLY_MODE="false" \
        mcp-server-test:latest

    echo "  [http/docker] Waiting for MCP server..."
    for i in $(seq 1 30); do
        code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${MCP_PORT}/mcp" 2>/dev/null || echo "000")
        if [[ -n "$code" && "$code" != "000" ]]; then
            echo "  MCP server ready (HTTP $code after ${i}s)"
            break
        fi
        sleep 1
    done

    echo "  [http/docker] Running tests..."
    local ret=0
    CB_CONNECTION_STRING="couchbase://127.0.0.1" \
    CB_USERNAME="$CB_USERNAME" \
    CB_PASSWORD="$CB_PASSWORD" \
    CB_MCP_TRANSPORT="http" \
    MCP_SERVER_URL="http://127.0.0.1:${MCP_PORT}/mcp" \
    CB_MCP_TEST_BUCKET="$CB_MCP_TEST_BUCKET" \
    CB_MCP_TEST_SCOPE="inventory" \
    CB_MCP_TEST_COLLECTION="airline" \
    PYTHONPATH=src \
        uv run pytest tests/integration -v --tb=short || ret=$?

    docker rm -f "$MCP_CONTAINER_NAME" 2>/dev/null || true
    return $ret
}

run_http_pypi_tests() {
    echo "  [http/pypi] Starting MCP server (PyPI binary)..."
    CB_CONNECTION_STRING="couchbase://127.0.0.1" \
    CB_USERNAME="$CB_USERNAME" \
    CB_PASSWORD="$CB_PASSWORD" \
    CB_MCP_TRANSPORT="http" \
    CB_MCP_HOST="127.0.0.1" \
    CB_MCP_PORT="$MCP_PORT" \
    CB_MCP_READ_ONLY_MODE="false" \
        "$PYPI_VENV/bin/couchbase-mcp-server" &
    local server_pid=$!
    BACKGROUND_PIDS+=("$server_pid")

    echo "  [http/pypi] Waiting for MCP server..."
    for i in $(seq 1 30); do
        if nc -z 127.0.0.1 "$MCP_PORT" 2>/dev/null; then break; fi
        sleep 1
    done

    echo "  [http/pypi] Running tests..."
    local ret=0
    CB_CONNECTION_STRING="couchbase://127.0.0.1" \
    CB_USERNAME="$CB_USERNAME" \
    CB_PASSWORD="$CB_PASSWORD" \
    CB_MCP_TRANSPORT="http" \
    MCP_SERVER_URL="http://127.0.0.1:${MCP_PORT}/mcp" \
    CB_MCP_TEST_BUCKET="$CB_MCP_TEST_BUCKET" \
    CB_MCP_TEST_SCOPE="inventory" \
    CB_MCP_TEST_COLLECTION="airline" \
    PYTHONPATH=src \
        uv run pytest tests/integration -v --tb=short || ret=$?

    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    return $ret
}

# ---------------------------------------------------------------------------
# Pre-build binaries that need it (once, before the version loop)
# ---------------------------------------------------------------------------
for binary in $SERVER_BINARIES; do
    case "$binary" in
        docker) build_docker_image ;;
        pypi)   build_pypi_wheel ;;
    esac
done

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
echo ""
echo "======================================================"
echo " MCP Server Local Test Matrix"
echo " Versions:   $CB_VERSIONS"
echo " Transports: $TRANSPORTS"
echo " Binaries:   $SERVER_BINARIES"
echo "======================================================"

for version in $CB_VERSIONS; do
    start_couchbase "$version"
    initialize_cluster
    load_sample_bucket
    set_indexer_mode
    setup_test_data

    for transport in $TRANSPORTS; do
        for binary in $SERVER_BINARIES; do
            # stdio is only tested with source binary
            if [[ "$transport" == "stdio" && "$binary" != "source" ]]; then
                continue
            fi

            echo ""
            echo "--------------------------------------------"
            echo " Testing: cb-$version / $transport / $binary"
            echo "--------------------------------------------"

            ret=0
            case "$transport/$binary" in
                stdio/source)  run_stdio_tests        || ret=$? ;;
                http/source)   run_http_source_tests  || ret=$? ;;
                http/docker)   run_http_docker_tests  || ret=$? ;;
                http/pypi)     run_http_pypi_tests    || ret=$? ;;
                *)             echo "Unknown combination: $transport/$binary"; ret=1 ;;
            esac

            record_result "$version" "$transport" "$binary" "$ret"
        done
    done

    stop_container
done

# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------
echo ""
echo ""
echo "======================================================"
echo " RESULTS SUMMARY"
echo "======================================================"
printf "%-8s | %-12s | %-10s | %-10s\n" "Status" "CB Version" "Transport" "Binary"
printf "%-8s-+-%-12s-+-%-10s-+-%-10s\n" "--------" "------------" "----------" "----------"

any_failed=0
for entry in "${RESULTS[@]}"; do
    IFS='|' read -r result_status cb_ver transport_name binary_name <<< "$entry"
    if [[ "$result_status" == "PASS" ]]; then
        printf "  %-6s | %-12s | %-10s | %-10s\n" "✅ PASS" "$cb_ver" "$transport_name" "$binary_name"
    else
        printf "  %-6s | %-12s | %-10s | %-10s\n" "❌ FAIL" "$cb_ver" "$transport_name" "$binary_name"
        any_failed=1
    fi
done

echo "======================================================"
echo ""

if [[ "$any_failed" -eq 1 ]]; then
    echo "Some combinations FAILED."
    exit 1
else
    echo "All combinations PASSED."
    exit 0
fi
