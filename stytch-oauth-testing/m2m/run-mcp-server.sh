#!/usr/bin/env bash
# Boot the Couchbase MCP server in streamable-http mode, wired to your Stytch
# project as the OAuth resource server (token verification only — no PRM).
#
# Edit the three Couchbase connection values below, then run this script.

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

# --- Stytch M2M -> MCP resource-server settings -----------------------------
export CB_MCP_OAUTH_JWT_JWKS_URI="https://${STYTCH_ENV}.stytch.com/v1/public/${STYTCH_PROJECT_ID}/.well-known/jwks.json"
export CB_MCP_OAUTH_JWT_ISSUER="stytch.com/${STYTCH_PROJECT_ID}"   # NB: no https:// scheme
export CB_MCP_OAUTH_JWT_AUDIENCE="${STYTCH_PROJECT_ID}"            # M2M aud == project id
# CB_MCP_OAUTH_JWT_ALGORITHM defaults to RS256 (Stytch signs M2M with RS256).
# Intentionally NOT setting CB_MCP_OAUTH_MCP_BASE_URL: PRM needs a URL issuer,
# but Stytch's issuer is scheme-less, so PRM would refuse to boot.

# --- Couchbase cluster (EDIT THESE) -----------------------------------------
CONNECTION_STRING="${CB_CONNECTION_STRING:-couchbases://localhost}"
USERNAME="${CB_USERNAME:-Administrator}"
PASSWORD="${CB_PASSWORD:-password}"

echo "JWKS:   $CB_MCP_OAUTH_JWT_JWKS_URI"
echo "Issuer: $CB_MCP_OAUTH_JWT_ISSUER"
echo "Aud:    $CB_MCP_OAUTH_JWT_AUDIENCE"
echo

# Run from the repo root so uv resolves the project.
cd ../..
exec uv run mcp-server \
  --transport=http \
  --connection-string="$CONNECTION_STRING" \
  --username="$USERNAME" \
  --password="$PASSWORD"
