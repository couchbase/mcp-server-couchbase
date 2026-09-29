#!/bin/bash
# Configures an already-running Operational Insights cluster: sets S3Mock as
# blob storage, then initializes the cluster — steps 4-5 of the quick-install
# guide (steps 2-3, booting the containers, are the caller's job — see
# docker-compose.yml):
# https://docs.couchbase.com/enterprise-analytics/current/intro/do-a-quick-install.html
#
# Every variable referenced below (OI_ADMIN_PORT, S3MOCK_BUCKET,
# BLOB_STORAGE_*, CLUSTER_*) must already be set in the environment — see
# oi-integration-tests.yml's job-level `env:` for the values this repo uses.
set -e

echo "Configuring blob storage (S3Mock)..."
curl -sf -X POST "http://localhost:${OI_ADMIN_PORT}/settings/analytics" \
  -d blobStorageScheme="$BLOB_STORAGE_SCHEME" \
  -d blobStorageBucket="$S3MOCK_BUCKET" \
  -d blobStorageRegion="$BLOB_STORAGE_REGION" \
  -d blobStorageEndpoint="$BLOB_STORAGE_ENDPOINT" \
  -d blobStorageAnonymousAuth="$BLOB_STORAGE_ANONYMOUS_AUTH" \
  -d blobStoragePathStyleAddressing="$BLOB_STORAGE_PATH_STYLE_ADDRESSING" \
  -d numStoragePartitions="$NUM_STORAGE_PARTITIONS"

echo ""
echo "Initializing cluster..."
curl -sf -X POST "http://localhost:${OI_ADMIN_PORT}/clusterInit" \
  -d username="$CLUSTER_USERNAME" \
  -d password="$CLUSTER_PASSWORD" \
  -d port=SAME \
  -d memoryQuota="$CLUSTER_MEMORY_QUOTA" \
  -d clusterName="$CLUSTER_NAME"

echo ""
echo "Waiting for the query service to accept real queries..."
# clusterInit returning success does not mean the query-serving path is
# ready yet: measured empirically, the first ~6 real query attempts after a
# fresh clusterInit get "Connection reset by peer" / "Server disconnected
# without sending a response" over roughly 10s, before it starts succeeding
# reliably. Uses the actual SDK (not a hand-crafted curl) so this probes
# the exact wire format the real tests use, rather than a guess at it.
uv run --extra dev python3 - <<PYEOF
import sys
import time

from couchbase_operational_insights.cluster import Cluster
from couchbase_operational_insights.credential import Credential

endpoint = "http://127.0.0.1:${OI_ANALYTICS_PORT}"
credential = Credential.from_username_and_password("${CLUSTER_USERNAME}", "${CLUSTER_PASSWORD}")

deadline = time.monotonic() + 60
attempt = 0
last_error = None
while time.monotonic() < deadline:
    attempt += 1
    try:
        cluster = Cluster.create_instance(endpoint, credential)
        list(cluster.execute_query("SELECT 1 AS one"))
        cluster.shutdown()
        print(f"Query service ready after {attempt} attempt(s)")
        sys.exit(0)
    except Exception as exc:
        last_error = exc
        time.sleep(2)

print(f"Query service never became ready after {attempt} attempts: {last_error}")
sys.exit(1)
PYEOF
