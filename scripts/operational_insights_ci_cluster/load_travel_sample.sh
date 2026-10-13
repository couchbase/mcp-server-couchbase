#!/bin/bash
# Seeds a travel-sample-shaped dataset into an already-configured
# Operational Insights cluster, for the accuracy tier.
#
# Why this exists: the accuracy tier's schema-inference cases sample real
# document content (ARRAY_INFER_SCHEMA), so they need a populated
# collection. The OI integration tests create their own throwaway
# collections and need nothing here.
#
# Why DDL rather than /sampleBuckets/install: the enterprise-analytics image
# caps the data service quota at 100MB — a clusterInit (or a later
# /pools/default POST) asking for more is rejected outright with "The data
# service quota (NNNMB) cannot be greater than 100MB", whatever the host's
# RAM. /sampleBuckets/install in turn refuses to run below a 200MB quota, so
# on this image that endpoint is unreachable by construction. Creating the
# collection with SQL++ and inserting rows sidesteps the data service
# entirely: the datasets land as INTERNAL analytics storage, which is also
# how a travel-sample dataset appears on a cluster that has one.
#
# The rows below are real travel-sample `inventory.airline` documents, kept
# to a handful: the accuracy cases infer a schema and check which *fields*
# come back, so document count is irrelevant and a small seed keeps the
# workflow fast.
#
# Variables it reads (OI_ANALYTICS_PORT, CLUSTER_USERNAME, CLUSTER_PASSWORD)
# must already be set — see accuracy.yml's job-level `env:`.
set -euo pipefail

DATABASE="${SAMPLE_DATABASE:-travel-sample}"
SCOPE="${SAMPLE_SCOPE:-inventory}"
COLLECTION="${SAMPLE_COLLECTION:-airline}"

echo "Seeding ${DATABASE}.${SCOPE}.${COLLECTION}..."

uv run --extra dev python3 - <<PYEOF
import sys
import time

from couchbase_operational_insights.cluster import Cluster
from couchbase_operational_insights.credential import Credential

endpoint = "http://127.0.0.1:${OI_ANALYTICS_PORT}"
credential = Credential.from_username_and_password(
    "${CLUSTER_USERNAME}", "${CLUSTER_PASSWORD}"
)
database, scope, collection = "${DATABASE}", "${SCOPE}", "${COLLECTION}"

# SQL++ quotes identifiers with backticks, but a backtick inside this
# heredoc would be read by bash as command substitution. Building them from
# chr(96) keeps the generated SQL correct without any escaping to get wrong.
BT = chr(96)


def ident(*parts):
    """Backtick-quote each part and join with dots, e.g. db.scope.coll."""
    return ".".join(f"{BT}{part}{BT}" for part in parts)


ks = ident(database, scope, collection)

# Real travel-sample inventory.airline documents. The field set (id, type,
# name, iata, icao, callsign, country) is what the schema cases assert on.
ROWS = [
    {"id": "10", "type": "airline", "name": "40-Mile Air", "iata": "Q5",
     "icao": "MLA", "callsign": "MILE-AIR", "country": "United States"},
    {"id": "10123", "type": "airline", "name": "Texas Wings", "iata": "TQ",
     "icao": "TXW", "callsign": "TXW", "country": "United States"},
    {"id": "10226", "type": "airline", "name": "Air Austral", "iata": "UU",
     "icao": "REU", "callsign": "REUNION", "country": "France"},
    {"id": "10748", "type": "airline", "name": "Locair", "iata": "ZQ",
     "icao": "LOC", "callsign": "LOCAIR", "country": "United States"},
    {"id": "10765", "type": "airline", "name": "SeaPort Airlines", "iata": "K5",
     "icao": "SQH", "callsign": "SASQUATCH", "country": "United States"},
]

def rows_to_values(rows):
    import json
    return ", ".join(json.dumps(row) for row in rows)

# The query service can still be settling right after clusterInit, so each
# statement is retried rather than failing the workflow on a transient reset.
def run(cluster, statement):
    deadline = time.monotonic() + 120
    last = None
    while time.monotonic() < deadline:
        try:
            return list(cluster.execute_query(statement))
        except Exception as exc:  # noqa: BLE001 - surface whatever the SDK raised
            last = exc
            time.sleep(3)
    raise RuntimeError(f"statement never succeeded: {statement!r} ({last})")

cluster = Cluster.create_instance(endpoint, credential)
try:
    run(cluster, f"CREATE DATABASE {ident(database)} IF NOT EXISTS;")
    run(cluster, f"CREATE SCOPE {ident(database, scope)} IF NOT EXISTS;")
    run(
        cluster,
        f"CREATE COLLECTION {ks} IF NOT EXISTS PRIMARY KEY (id: string);",
    )
    # Idempotent: re-running the workflow against a reused cluster should not
    # double the rows, and UPSERT keys on the declared primary key.
    run(cluster, f"UPSERT INTO {ks} ([{rows_to_values(ROWS)}]);")

    count = run(cluster, f"SELECT COUNT(*) AS n FROM {ks};")
    docs = count[0].get("n", 0) if count else 0
    if docs <= 0:
        print(f"::error::{database}.{scope}.{collection} is empty after seeding")
        sys.exit(1)
    print(f"{database}.{scope}.{collection} ready with {docs} document(s)")
finally:
    cluster.shutdown()
PYEOF

echo "Seed complete."
