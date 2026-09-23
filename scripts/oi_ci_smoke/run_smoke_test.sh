#!/bin/bash
# Boots the OI + S3Mock containers and prints proof they can reach each
# other by hostname over the shared docker-compose network. Plumbing-only:
# proves the containers + network are up, nothing about Operational
# Insights functionality itself yet.
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"

docker compose up -d --wait

echo "oi -> s3mock: $(docker compose exec -T oi curl -s -o /dev/null -w '%{http_code}' http://s3mock:9090)"
echo "s3mock -> oi: $(docker compose exec -T s3mock wget -q -S --spider http://oi:8091 2>&1 | head -1)"

docker compose down -v
