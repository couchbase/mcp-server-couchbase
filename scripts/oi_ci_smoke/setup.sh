#!/bin/bash
# Boots the OI + S3Mock containers, configures Enterprise Analytics to use
# S3Mock as its blob storage, and initializes the cluster — steps 2-5 of the
# quick-install guide:
# https://docs.couchbase.com/enterprise-analytics/current/intro/do-a-quick-install.html
# Does not tear down — run `docker compose down -v` from this directory
# when done.
set -e

OI_IMAGE="couchbase/enterprise-analytics:2.2.1"
S3MOCK_IMAGE="adobe/s3mock:5.2.3"
S3MOCK_BUCKET="cloud-storage-container"
S3MOCK_STORE_ROOT="fs"
S3MOCK_RETAIN_FILES_ON_EXIT="true"
OI_ADMIN_PORT=8091
OI_ANALYTICS_PORT=8095

BLOB_STORAGE_SCHEME="s3"
BLOB_STORAGE_REGION="us-east-1"
BLOB_STORAGE_ENDPOINT="http://s3mock:9090"
BLOB_STORAGE_ANONYMOUS_AUTH="true"
BLOB_STORAGE_PATH_STYLE_ADDRESSING="true"
NUM_STORAGE_PARTITIONS=16

CLUSTER_USERNAME="Administrator"
CLUSTER_PASSWORD="password"
CLUSTER_MEMORY_QUOTA=100
CLUSTER_NAME="OI CI Smoke Cluster"

cd "$(dirname "${BASH_SOURCE[0]}")"

# Written to .env, not just exported: docker-compose.yml's ${VAR} references
# need to resolve on every future `docker compose` call in this directory
# (including a later `down`, from a shell that never ran this script), and
# Compose auto-loads .env from the compose file's own directory regardless
# of which shell invokes it.
cat > .env <<EOF
OI_IMAGE=$OI_IMAGE
S3MOCK_IMAGE=$S3MOCK_IMAGE
S3MOCK_BUCKET=$S3MOCK_BUCKET
S3MOCK_STORE_ROOT=$S3MOCK_STORE_ROOT
S3MOCK_RETAIN_FILES_ON_EXIT=$S3MOCK_RETAIN_FILES_ON_EXIT
OI_ADMIN_PORT=$OI_ADMIN_PORT
OI_ANALYTICS_PORT=$OI_ANALYTICS_PORT
EOF

docker compose up -d --wait

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
