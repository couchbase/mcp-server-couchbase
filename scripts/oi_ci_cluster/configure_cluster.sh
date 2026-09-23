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
