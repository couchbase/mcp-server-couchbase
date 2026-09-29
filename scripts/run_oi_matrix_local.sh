#!/usr/bin/env bash
# =============================================================================
# Local OI Test Matrix Runner
# =============================================================================
# Runs Operational Insights integration tests across transport x server
# binary combinations. Structural mirror of scripts/run_matrix_local.sh, but
# drives the OI cluster's docker-compose lifecycle (scripts/oi_ci_cluster/)
# instead of a raw Couchbase Server container. No cb_version axis: the OI
# image is pinned, not swept.
#
# Prerequisites:
#   - Docker installed and running
#   - uv installed
#   - Project dependencies installed (uv sync --extra dev)
#
# Usage:
#   ./scripts/run_oi_matrix_local.sh
#
# Optional: run a subset for debugging:
#   TRANSPORTS="http" SERVER_BINARIES="docker" ./scripts/run_oi_matrix_local.sh
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
TRANSPORTS="${TRANSPORTS:-stdio http}"
SERVER_BINARIES="${SERVER_BINARIES:-source docker pypi}"

# docker-compose.yml's ${VAR} references (steps 2-3) — same defaults as
# oi-integration-tests.yml and tests/README.md's local-setup snippet.
export OI_IMAGE="${OI_IMAGE:-couchbase/enterprise-analytics:2.2.1}"
export S3MOCK_IMAGE="${S3MOCK_IMAGE:-adobe/s3mock:5.2.3}"
export S3MOCK_BUCKET="${S3MOCK_BUCKET:-cloud-storage-container}"
export S3MOCK_STORE_ROOT="${S3MOCK_STORE_ROOT:-fs}"
export S3MOCK_RETAIN_FILES_ON_EXIT="${S3MOCK_RETAIN_FILES_ON_EXIT:-true}"
export OI_ADMIN_PORT="${OI_ADMIN_PORT:-8091}"
export OI_ANALYTICS_PORT="${OI_ANALYTICS_PORT:-8095}"

# configure_cluster.sh's blob storage settings (step 4).
export BLOB_STORAGE_SCHEME="${BLOB_STORAGE_SCHEME:-s3}"
export BLOB_STORAGE_REGION="${BLOB_STORAGE_REGION:-us-east-1}"
export BLOB_STORAGE_ENDPOINT="${BLOB_STORAGE_ENDPOINT:-http://s3mock:9090}"
export BLOB_STORAGE_ANONYMOUS_AUTH="${BLOB_STORAGE_ANONYMOUS_AUTH:-true}"
export BLOB_STORAGE_PATH_STYLE_ADDRESSING="${BLOB_STORAGE_PATH_STYLE_ADDRESSING:-true}"
export NUM_STORAGE_PARTITIONS="${NUM_STORAGE_PARTITIONS:-16}"

# configure_cluster.sh's clusterInit settings (step 5).
export CLUSTER_USERNAME="${CLUSTER_USERNAME:-Administrator}"
export CLUSTER_PASSWORD="${CLUSTER_PASSWORD:-password}"
export CLUSTER_MEMORY_QUOTA="${CLUSTER_MEMORY_QUOTA:-100}"
export CLUSTER_NAME="${CLUSTER_NAME:-OI Local Cluster}"

COMPOSE_PROJECT="oi-mcp-test"
MCP_CONTAINER_NAME="mcp-server-oi-http-test"
PYPI_VENV="/tmp/cb-mcp-oi-pypi-venv"
MCP_PORT=8001

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
COMPOSE_FILE="scripts/oi_ci_cluster/docker-compose.yml"

# Resolved once the OI cluster is up (see start_oi_cluster) — the network
# Compose actually created for this project, looked up rather than assumed:
# Compose's default-network-naming convention isn't worth hardcoding blind.
COMPOSE_NETWORK=""

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
    docker rm -f "$MCP_CONTAINER_NAME" 2>/dev/null || true
    docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    rm -rf "$PYPI_VENV"
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
declare -a RESULTS=()

record_result() {
    local transport="$1" binary="$2" exit_code="$3"
    if [[ "$exit_code" -eq 0 ]]; then
        RESULTS+=("PASS|$transport|$binary")
    else
        RESULTS+=("FAIL|$transport|$binary")
    fi
}

# ---------------------------------------------------------------------------
# OI cluster lifecycle helpers
# ---------------------------------------------------------------------------
start_oi_cluster() {
    echo ""
    echo "============================================"
    echo " Starting Operational Insights cluster"
    echo "============================================"
    docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" up -d --wait

    COMPOSE_NETWORK=$(docker network ls \
        --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
        --format '{{.Name}}' | head -n1)
    if [[ -z "$COMPOSE_NETWORK" ]]; then
        echo "  ERROR: could not resolve the Compose network for project $COMPOSE_PROJECT" >&2
        exit 1
    fi
    echo "  Compose network: $COMPOSE_NETWORK"
}

configure_oi_cluster() {
    echo "Configuring Operational Insights cluster..."
    ./scripts/oi_ci_cluster/configure_cluster.sh
}

stop_oi_cluster() {
    docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" down -v 2>/dev/null || true
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
# Test runners per transport x binary
# ---------------------------------------------------------------------------
run_stdio_tests() {
    echo "  [stdio/source] Running tests..."
    CB_OI_CONNECTION_STRING="http://127.0.0.1:${OI_ANALYTICS_PORT}" \
    CB_OI_USERNAME="$CLUSTER_USERNAME" \
    CB_OI_PASSWORD="$CLUSTER_PASSWORD" \
    CB_MCP_TRANSPORT="stdio" \
    PYTHONPATH=src \
        uv run pytest tests/integration/operational_insights -v --tb=short
}

run_http_source_tests() {
    echo "  [http/source] Starting MCP server..."
    # Not containerized, so it reaches the OI cluster the same way a local
    # dev shell would: via the host-published Analytics port. 127.0.0.1, not
    # localhost — see tests/README.md's note on the SDK's ::1 fallback.
    CB_OI_CONNECTION_STRING="http://127.0.0.1:${OI_ANALYTICS_PORT}" \
    CB_OI_USERNAME="$CLUSTER_USERNAME" \
    CB_OI_PASSWORD="$CLUSTER_PASSWORD" \
    CB_MCP_TRANSPORT="http" \
    CB_MCP_HOST="127.0.0.1" \
    CB_MCP_PORT="$MCP_PORT" \
    CB_MCP_READ_ONLY_MODE="false" \
    PYTHONPATH=src \
        uv run python -m mcp_server operational-insights &
    local server_pid=$!
    BACKGROUND_PIDS+=("$server_pid")

    for i in $(seq 1 30); do
        if nc -z 127.0.0.1 "$MCP_PORT" 2>/dev/null; then break; fi
        sleep 1
    done

    echo "  [http/source] Running tests..."
    local ret=0
    CB_OI_CONNECTION_STRING="http://127.0.0.1:${OI_ANALYTICS_PORT}" \
    CB_OI_USERNAME="$CLUSTER_USERNAME" \
    CB_OI_PASSWORD="$CLUSTER_PASSWORD" \
    CB_MCP_TRANSPORT="http" \
    MCP_SERVER_URL="http://127.0.0.1:${MCP_PORT}/mcp" \
    PYTHONPATH=src \
        uv run pytest tests/integration/operational_insights -v --tb=short || ret=$?

    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    return $ret
}

run_http_docker_tests() {
    echo "  [http/docker] Starting MCP server container..."
    docker rm -f "$MCP_CONTAINER_NAME" 2>/dev/null || true
    # Join the compose stack's own network and reach the OI cluster by its
    # service name ("oi") on the container-internal Analytics port (8095) —
    # not --network host (unreliable on Docker Desktop for macOS/Windows)
    # and not the host-published OI_ANALYTICS_PORT (only meaningful from the
    # host's own network namespace, not from inside another container).
    docker run -d --name "$MCP_CONTAINER_NAME" \
        --network "$COMPOSE_NETWORK" \
        -p "${MCP_PORT}:${MCP_PORT}" \
        -e CB_OI_CONNECTION_STRING="http://oi:8095" \
        -e CB_OI_USERNAME="$CLUSTER_USERNAME" \
        -e CB_OI_PASSWORD="$CLUSTER_PASSWORD" \
        -e CB_MCP_TRANSPORT="http" \
        -e CB_MCP_HOST="0.0.0.0" \
        -e CB_MCP_PORT="$MCP_PORT" \
        -e CB_MCP_READ_ONLY_MODE="false" \
        mcp-server-test:latest operational-insights

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
    CB_OI_CONNECTION_STRING="http://127.0.0.1:${OI_ANALYTICS_PORT}" \
    CB_OI_USERNAME="$CLUSTER_USERNAME" \
    CB_OI_PASSWORD="$CLUSTER_PASSWORD" \
    CB_MCP_TRANSPORT="http" \
    MCP_SERVER_URL="http://127.0.0.1:${MCP_PORT}/mcp" \
    PYTHONPATH=src \
        uv run pytest tests/integration/operational_insights -v --tb=short || ret=$?

    docker rm -f "$MCP_CONTAINER_NAME" 2>/dev/null || true
    return $ret
}

run_http_pypi_tests() {
    echo "  [http/pypi] Starting MCP server (PyPI binary)..."
    CB_OI_CONNECTION_STRING="http://127.0.0.1:${OI_ANALYTICS_PORT}" \
    CB_OI_USERNAME="$CLUSTER_USERNAME" \
    CB_OI_PASSWORD="$CLUSTER_PASSWORD" \
    CB_MCP_TRANSPORT="http" \
    CB_MCP_HOST="127.0.0.1" \
    CB_MCP_PORT="$MCP_PORT" \
    CB_MCP_READ_ONLY_MODE="false" \
        "$PYPI_VENV/bin/couchbase-mcp-server" operational-insights &
    local server_pid=$!
    BACKGROUND_PIDS+=("$server_pid")

    echo "  [http/pypi] Waiting for MCP server..."
    for i in $(seq 1 30); do
        if nc -z 127.0.0.1 "$MCP_PORT" 2>/dev/null; then break; fi
        sleep 1
    done

    echo "  [http/pypi] Running tests..."
    local ret=0
    CB_OI_CONNECTION_STRING="http://127.0.0.1:${OI_ANALYTICS_PORT}" \
    CB_OI_USERNAME="$CLUSTER_USERNAME" \
    CB_OI_PASSWORD="$CLUSTER_PASSWORD" \
    CB_MCP_TRANSPORT="http" \
    MCP_SERVER_URL="http://127.0.0.1:${MCP_PORT}/mcp" \
    PYTHONPATH=src \
        uv run pytest tests/integration/operational_insights -v --tb=short || ret=$?

    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
    return $ret
}

# ---------------------------------------------------------------------------
# Pre-build binaries that need it (once, before the loop)
# ---------------------------------------------------------------------------
for binary in $SERVER_BINARIES; do
    case "$binary" in
        docker) build_docker_image ;;
        pypi)   build_pypi_wheel ;;
    esac
done

# ---------------------------------------------------------------------------
# Cluster lifecycle (once — no cb_version axis for OI)
# ---------------------------------------------------------------------------
start_oi_cluster
configure_oi_cluster

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
echo ""
echo "======================================================"
echo " OI MCP Server Local Test Matrix"
echo " Transports: $TRANSPORTS"
echo " Binaries:   $SERVER_BINARIES"
echo "======================================================"

for transport in $TRANSPORTS; do
    for binary in $SERVER_BINARIES; do
        # stdio is only tested with source binary
        if [[ "$transport" == "stdio" && "$binary" != "source" ]]; then
            continue
        fi

        echo ""
        echo "--------------------------------------------"
        echo " Testing: $transport / $binary"
        echo "--------------------------------------------"

        ret=0
        case "$transport/$binary" in
            stdio/source)  run_stdio_tests        || ret=$? ;;
            http/source)   run_http_source_tests  || ret=$? ;;
            http/docker)   run_http_docker_tests  || ret=$? ;;
            http/pypi)     run_http_pypi_tests    || ret=$? ;;
            *)             echo "Unknown combination: $transport/$binary"; ret=1 ;;
        esac

        record_result "$transport" "$binary" "$ret"
    done
done

stop_oi_cluster

# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------
echo ""
echo ""
echo "======================================================"
echo " RESULTS SUMMARY"
echo "======================================================"
printf "%-8s | %-10s | %-10s\n" "Status" "Transport" "Binary"
printf "%-8s-+-%-10s-+-%-10s\n" "--------" "----------" "----------"

any_failed=0
for entry in "${RESULTS[@]}"; do
    IFS='|' read -r result_status transport_name binary_name <<< "$entry"
    if [[ "$result_status" == "PASS" ]]; then
        printf "  %-6s | %-10s | %-10s\n" "✅ PASS" "$transport_name" "$binary_name"
    else
        printf "  %-6s | %-10s | %-10s\n" "❌ FAIL" "$transport_name" "$binary_name"
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
