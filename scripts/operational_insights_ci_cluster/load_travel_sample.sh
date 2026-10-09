#!/bin/bash
# Loads the travel-sample dataset into an already-configured Operational
# Insights cluster, for the accuracy tier (oi-accuracy.yml).
#
# Why this exists at all: the OI *integration* tests create their own
# throwaway scopes and collections, so they need no sample data. The
# accuracy tier's schema-inference cases are different — ARRAY_INFER_SCHEMA
# samples real document content, so they need a collection that is already
# populated. tests/accuracy/operational_insights/conftest.py defaults to
# travel-sample's inventory.airline for exactly that reason.
#
# An OI node serves the same /sampleBuckets endpoints a Couchbase Server
# node does, so installing is one POST. The two things worth knowing:
#
#   1. It needs a >=200MB cluster memory quota, and the quota cannot be
#      raised after clusterInit. oi-accuracy.yml therefore sets
#      CLUSTER_MEMORY_QUOTA=1024 up front. If this script reports a quota
#      error, that value is the thing to change — not this script.
#   2. The POST returns as soon as the load is *accepted*. The collections
#      appear in System.Metadata.`Dataset` only once ingestion finishes, so
#      the wait below polls for the data rather than sleeping a fixed time.
#
# Every variable it reads (OI_ADMIN_PORT, OI_ANALYTICS_PORT,
# CLUSTER_USERNAME, CLUSTER_PASSWORD) must already be set — see
# oi-accuracy.yml's job-level `env:`.
set -euo pipefail

SAMPLE_NAME="${SAMPLE_NAME:-travel-sample}"
# The collection the accuracy tier infers a schema from. Checked explicitly
# so a partial load (dataset present, this collection still ingesting) is
# caught here rather than as a confusing test failure later.
SAMPLE_COLLECTION="${SAMPLE_COLLECTION:-airline}"

echo "Installing ${SAMPLE_NAME} sample dataset..."
RESPONSE=$(curl -s -w '\n%{http_code}' -X POST \
  "http://localhost:${OI_ADMIN_PORT}/sampleBuckets/install" \
  -u "${CLUSTER_USERNAME}:${CLUSTER_PASSWORD}" \
  -H "Content-Type: application/json" \
  -d "[\"${SAMPLE_NAME}\"]")

STATUS=$(echo "$RESPONSE" | tail -n 1)
BODY=$(echo "$RESPONSE" | sed '$d')

# Only 4xx/5xx are treated as fatal here, rather than allow-listing the
# success codes: which 2xx an accepted install returns is a server detail
# (200 vs 202 varies by version), and guessing wrong would fail a load that
# actually worked. A genuinely broken install is still caught, either by the
# explicit 4xx/5xx check below or by the ingestion poll that follows — the
# poll is the real gate, since it verifies queryable documents rather than
# trusting any status code.
case "$STATUS" in
  4??|5??)
    echo "::error::Sample install failed (HTTP ${STATUS}): ${BODY}"
    echo "A quota error here means CLUSTER_MEMORY_QUOTA is too low: it must be"
    echo ">=200MB, and must be set at clusterInit time since the data service"
    echo "refuses to raise it afterwards."
    exit 1
    ;;
esac

echo "Install accepted (HTTP ${STATUS}). Waiting for ingestion to finish..."

# Poll the catalog through the SDK rather than curl: this is the same wire
# path the tests use, so a cluster that answers REST but not queries is
# caught here instead of mid-eval.
uv run --extra dev python3 - <<PYEOF
import sys
import time

from couchbase_operational_insights.cluster import Cluster
from couchbase_operational_insights.credential import Credential

endpoint = "http://127.0.0.1:${OI_ANALYTICS_PORT}"
credential = Credential.from_username_and_password(
    "${CLUSTER_USERNAME}", "${CLUSTER_PASSWORD}"
)
database = "${SAMPLE_NAME}"
collection = "${SAMPLE_COLLECTION}"

query = (
    'SELECT COUNT(*) AS n FROM System.Metadata.\`Dataset\` d '
    'WHERE d.DatabaseName = "' + database + '" '
    'AND d.DatasetName = "' + collection + '";'
)

# Sample ingestion on a cold cluster is slow; 300s is generous on purpose,
# since a timeout here fails the whole workflow.
deadline = time.monotonic() + 300
attempt = 0
last_error = None
cluster = None

while time.monotonic() < deadline:
    attempt += 1
    try:
        if cluster is None:
            cluster = Cluster.create_instance(endpoint, credential)
        rows = list(cluster.execute_query(query))
        if rows and rows[0].get("n", 0) > 0:
            # The catalog entry exists; confirm the collection actually has
            # documents, since schema inference on an empty one is useless.
            count_rows = list(
                cluster.execute_query(
                    f'SELECT COUNT(*) AS n FROM \`{database}\`.\`inventory\`.\`{collection}\`;'
                )
            )
            docs = count_rows[0].get("n", 0) if count_rows else 0
            if docs > 0:
                print(
                    f"{database}.inventory.{collection} ready with {docs} "
                    f"document(s) after {attempt} attempt(s)"
                )
                cluster.shutdown()
                sys.exit(0)
            last_error = f"collection present but empty ({docs} docs)"
        else:
            last_error = "dataset not in catalog yet"
    except Exception as exc:  # noqa: BLE001 - report whatever the SDK raised
        last_error = exc
        # Drop the handle so a reset connection is rebuilt next attempt.
        cluster = None
    time.sleep(3)

print(
    f"::error::{database}.inventory.{collection} never became queryable "
    f"after {attempt} attempt(s): {last_error}"
)
sys.exit(1)
PYEOF

echo "${SAMPLE_NAME} loaded."
