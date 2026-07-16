#!/usr/bin/env bash
# Create the four M2M test clients (read / write / both / none) in your Stytch
# project via the management API — NO dashboard UI required.
#
# Auth: HTTP Basic with PROJECT_ID:PROJECT_SECRET (the management secret from
#       Stytch → Project settings → API keys).
# Endpoint: POST https://<env>.stytch.com/v1/m2m/clients
#
# Stytch shows each client_secret exactly ONCE, at creation. This script
# captures it immediately and writes it back into .env.stytch.
#
# Usage:  ./setup-clients.sh

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env.stytch ]; then
  echo "ERROR: .env.stytch not found. Run: cp .env.stytch.example .env.stytch" >&2
  exit 1
fi
# shellcheck disable=SC1091
source .env.stytch

: "${STYTCH_ENV:?set STYTCH_ENV in .env.stytch}"
: "${STYTCH_PROJECT_ID:?set STYTCH_PROJECT_ID in .env.stytch}"
: "${STYTCH_PROJECT_SECRET:?set STYTCH_PROJECT_SECRET in .env.stytch}"

HOST="https://${STYTCH_ENV}.stytch.com"
CREATE_URL="${HOST}/v1/m2m/clients"

READ_SCOPE="couchbase-mcp:read"
WRITE_SCOPE="couchbase-mcp:write"
# "none" deliberately gets no Couchbase scopes so every tool is denied.
# If your Stytch project rejects an empty scopes array, change NONE_SCOPES_JSON
# to an unrelated dummy like '["noop:none"]' — the server ignores unknown scopes.
NONE_SCOPES_JSON='[]'

# create_client <label> <scopes-json-array> -> echoes "client_id client_secret"
create_client() {
  local label="$1" scopes_json="$2" resp client_id client_secret
  resp=$(curl -sS -X POST "$CREATE_URL" \
    -u "${STYTCH_PROJECT_ID}:${STYTCH_PROJECT_SECRET}" \
    -H "Content-Type: application/json" \
    -d "{\"client_name\":\"couchbase-mcp-test-${label}\",\"client_description\":\"Couchbase MCP OAuth test client (${label})\",\"scopes\":${scopes_json}}")

  if echo "$resp" | jq -e '.status_code? // empty | numbers | select(. >= 400)' >/dev/null 2>&1 \
     || echo "$resp" | jq -e '.error_type? // .error? // empty' >/dev/null 2>&1; then
    echo "Stytch error creating '${label}' client:" >&2
    echo "$resp" | jq . >&2
    exit 1
  fi

  client_id=$(echo "$resp" | jq -r '.m2m_client.client_id // .client_id // empty')
  client_secret=$(echo "$resp" | jq -r '.m2m_client.client_secret // .client_secret // empty')

  if [ -z "$client_id" ] || [ -z "$client_secret" ]; then
    echo "Could not parse client_id/client_secret for '${label}'. Raw response:" >&2
    echo "$resp" | jq . >&2
    exit 1
  fi
  echo "$client_id $client_secret"
}

echo "Creating M2M clients in Stytch project ${STYTCH_PROJECT_ID} (${STYTCH_ENV})..."

read -r READ_ID READ_SECRET   < <(create_client read  "[\"${READ_SCOPE}\"]")
echo "  ✓ read"
read -r WRITE_ID WRITE_SECRET < <(create_client write "[\"${WRITE_SCOPE}\"]")
echo "  ✓ write"
read -r BOTH_ID BOTH_SECRET   < <(create_client both  "[\"${READ_SCOPE}\",\"${WRITE_SCOPE}\"]")
echo "  ✓ both"
read -r NONE_ID NONE_SECRET   < <(create_client none  "${NONE_SCOPES_JSON}")
echo "  ✓ none"

# Rewrite the 8 client lines in .env.stytch (strip old, append fresh).
TMP=$(mktemp)
grep -vE '^(READ|WRITE|BOTH|NONE)_CLIENT_(ID|SECRET)=' .env.stytch > "$TMP"
{
  echo "READ_CLIENT_ID=${READ_ID}"
  echo "READ_CLIENT_SECRET=${READ_SECRET}"
  echo "WRITE_CLIENT_ID=${WRITE_ID}"
  echo "WRITE_CLIENT_SECRET=${WRITE_SECRET}"
  echo "BOTH_CLIENT_ID=${BOTH_ID}"
  echo "BOTH_CLIENT_SECRET=${BOTH_SECRET}"
  echo "NONE_CLIENT_ID=${NONE_ID}"
  echo "NONE_CLIENT_SECRET=${NONE_SECRET}"
} >> "$TMP"
mv "$TMP" .env.stytch

echo
echo "Done. Credentials written to .env.stytch. Mint a token with:"
echo "  ./stytch-mint.sh both --decode"
