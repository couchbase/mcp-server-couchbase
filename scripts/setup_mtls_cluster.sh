#!/usr/bin/env bash
# =============================================================================
# Start a single-node Couchbase that only accepts client certificates (mTLS)
# =============================================================================
# Used by the mtls-integration-tests CI job and runnable locally. It:
#   1. starts Couchbase in Docker, initializes it and loads travel-sample
#   2. creates a throwaway CA and a certificate for the node, and installs it
#   3. creates a client certificate (CN=mcp-mtls) and the Couchbase user it maps to
#   4. makes the cluster require client certificates ("mandatory" mode)
#
# After it runs, nothing can authenticate with a username and password over TLS;
# only the client certificate works. That is the setup the REST-backed tools
# once failed on (DA-2504).
#
# Usage:
#   ./scripts/setup_mtls_cluster.sh <out_dir> [container_name]
#
# Writes to <out_dir>: ca.pem, mcp-mtls.pem, mcp-mtls.key. Needs Docker, curl
# and openssl. COUCHBASE_IMAGE overrides the image (default 8.0.0).
# =============================================================================

set -euo pipefail

OUT_DIR="${1:?usage: $0 <out_dir> [container_name]}"
CONTAINER="${2:-cb-mtls}"
IMAGE="${COUCHBASE_IMAGE:-couchbase:enterprise-8.0.0}"
ADMIN_USER=Administrator
ADMIN_PASSWORD=password
CLIENT_CN=mcp-mtls

INBOX=/opt/couchbase/var/lib/couchbase/inbox
# 127.0.0.1, not "localhost": it resolves to ::1 first in some containers, and
# a node asked over ::1 renames itself to [::1], which its services don't use.
ADMIN_URL=http://127.0.0.1:8091

mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

# --- helpers -----------------------------------------------------------------

admin_curl() { curl -sS -u "$ADMIN_USER:$ADMIN_PASSWORD" "$@"; }

# Run inside the container. Installing a certificate or changing security
# settings restarts the node's listeners for a moment, so a command right after
# one can fail; retry, and show the CLI's own message (printed on stdout) if it
# never works.
cb_cli() {
    local out
    for _ in $(seq 1 10); do
        if out="$(docker exec "$CONTAINER" /opt/couchbase/bin/couchbase-cli "$1" \
            -c 127.0.0.1:8091 -u "$ADMIN_USER" -p "$ADMIN_PASSWORD" "${@:2}" 2>&1)"; then
            return 0
        fi
        sleep 3
    done
    echo "couchbase-cli $1 failed: $out" >&2
    return 1
}

wait_for() { # wait_for <description> <command...>
    local what="$1"
    shift
    for _ in $(seq 1 60); do
        "$@" > /dev/null 2>&1 && return 0
        sleep 2
    done
    echo "Timed out waiting for $what" >&2
    return 1
}

# --- 1. Couchbase: start, initialize, load travel-sample ----------------------

echo "Starting $CONTAINER ($IMAGE)..."
docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
    -p 8091-8096:8091-8096 -p 9102:9102 -p 11207:11207 -p 11210:11210 \
    -p 18091-18096:18091-18096 -p 19102:19102 "$IMAGE" > /dev/null
wait_for "Couchbase to start" curl -sf "$ADMIN_URL/pools"

curl -s -X POST "$ADMIN_URL/nodes/self/controller/settings" \
    -d 'path=/opt/couchbase/var/lib/couchbase/data' \
    -d 'index_path=/opt/couchbase/var/lib/couchbase/data' > /dev/null
curl -s -X POST "$ADMIN_URL/node/controller/setupServices" \
    -d 'services=kv,n1ql,index,fts' > /dev/null
curl -s -X POST "$ADMIN_URL/pools/default" \
    -d memoryQuota=512 -d indexMemoryQuota=256 -d ftsMemoryQuota=256 > /dev/null
curl -s -X POST "$ADMIN_URL/settings/web" \
    -d "username=$ADMIN_USER" -d "password=$ADMIN_PASSWORD" -d port=SAME > /dev/null

sleep 5
admin_curl -X POST "$ADMIN_URL/sampleBuckets/install" \
    -H 'Content-Type: application/json' -d '["travel-sample"]' > /dev/null
wait_for "travel-sample to load" \
    bash -c "curl -sf -u '$ADMIN_USER:$ADMIN_PASSWORD' $ADMIN_URL/pools/default/buckets/travel-sample | grep -q '\"status\":\"healthy\"'"
sleep 30 # the sample's indexes keep building after the bucket is healthy

# --- 2. PKI: a CA, and a certificate for the node -----------------------------

echo "Creating the CA and the node certificate..."
cat > ca.cnf <<EOF
[req]
distinguished_name = dn
x509_extensions = ca_ext
prompt = no
[dn]
CN = MCP Test CA
[ca_ext]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
EOF
openssl genrsa -out ca.key 2048 2> /dev/null
openssl req -new -x509 -days 30 -sha256 -key ca.key -out ca.pem -config ca.cnf

# The SAN lists every address a client may use. The SDK also reports the node
# by its Docker network IP, so that has to be there too.
node_ip="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$CONTAINER")"
cat > node.ext <<EOF
basicConstraints = CA:FALSE
extendedKeyUsage = serverAuth,clientAuth
keyUsage = digitalSignature,keyEncipherment
subjectAltName = DNS:localhost,IP:127.0.0.1,IP:::1,IP:$node_ip
EOF
openssl genrsa -out node.key 2048 2> /dev/null
openssl req -new -key node.key -out node.csr -subj "/CN=$CONTAINER"
openssl x509 -req -in node.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
    -days 30 -sha256 -extfile node.ext -out node.pem 2> /dev/null

# --- 3. Install the node certificate -------------------------------------------
# Couchbase picks files up from its inbox directory, by these exact names:
#   chain.pem (the node cert), pkey.key (its key), CA/ca.pem (who to trust).

docker exec -u root "$CONTAINER" mkdir -p "$INBOX/CA"
docker cp node.pem "$CONTAINER:$INBOX/chain.pem"
docker cp node.key "$CONTAINER:$INBOX/pkey.key"
docker cp ca.pem "$CONTAINER:$INBOX/CA/ca.pem"
docker exec -u root "$CONTAINER" chown -R couchbase "$INBOX"
docker exec -u root "$CONTAINER" chmod -R 0700 "$INBOX"

# Trust the CA first, then switch to the node certificate that chains to it.
admin_curl -f -X POST "$ADMIN_URL/node/controller/loadTrustedCAs" > /dev/null
admin_curl -f -X POST "$ADMIN_URL/node/controller/reloadCertificate" > /dev/null
wait_for "the node after loading its certificate" \
    curl -sf -u "$ADMIN_USER:$ADMIN_PASSWORD" "$ADMIN_URL/pools/default"

# --- 4. The client certificate, and the user it maps to ------------------------
# The certificate only proves who the caller is; the user's roles decide what
# it may do. Couchbase takes the user name from the certificate's CN.

echo "Creating the client certificate..."
cat > client.ext <<EOF
basicConstraints = CA:FALSE
extendedKeyUsage = clientAuth
keyUsage = digitalSignature
EOF
openssl genrsa -out "$CLIENT_CN.key" 2048 2> /dev/null
openssl req -new -key "$CLIENT_CN.key" -out "$CLIENT_CN.csr" -subj "/CN=$CLIENT_CN"
openssl x509 -req -in "$CLIENT_CN.csr" -CA ca.pem -CAkey ca.key -CAcreateserial \
    -days 30 -sha256 -extfile client.ext -out "$CLIENT_CN.pem" 2> /dev/null

cb_cli user-manage --set --rbac-username "$CLIENT_CN" \
    --rbac-password "Passw0rd!$RANDOM" --roles admin --auth-domain local

# --- 5. Require client certificates ---------------------------------------------
# "mandatory" also needs node-to-node encryption on, and Couchbase refuses to
# change that while auto-failover is enabled: turn it off, then restore it.

failover_timeout="$(admin_curl "$ADMIN_URL/settings/autoFailover" \
    | sed -n 's/.*"timeout":\([0-9]*\).*/\1/p')"
cb_cli setting-autofailover --enable-auto-failover 0
cb_cli node-to-node-encryption --enable
cb_cli setting-security --set --cluster-encryption-level all
cb_cli setting-autofailover --enable-auto-failover 1 \
    --auto-failover-timeout "${failover_timeout:-120}"

admin_curl -X POST "$ADMIN_URL/settings/clientCertAuth" \
    -H 'Content-Type: application/json' \
    -d '{"state":"mandatory","prefixes":[{"path":"subject.cn","prefix":"","delimiter":""}]}' \
    > /dev/null

echo "Done: client certificates are now required. Files are in $(pwd)"
echo "  CB_MCP_TEST_MTLS_CERT=$(pwd)/$CLIENT_CN.pem"
echo "  CB_MCP_TEST_MTLS_KEY=$(pwd)/$CLIENT_CN.key"
echo "  CB_MCP_TEST_MTLS_CA=$(pwd)/ca.pem"
