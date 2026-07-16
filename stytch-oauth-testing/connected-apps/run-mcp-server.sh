#!/usr/bin/env bash
# Boot the Couchbase MCP server in streamable-http mode as an OAuth resource
# server for Stytch Connected Apps, WITH Protected Resource Metadata (PRM) so
# DCR-capable clients (MCP Inspector) can discover Stytch automatically.
#
# Requires a Stytch CUSTOM DOMAIN so the token issuer is a real https URL —
# the server refuses to publish PRM with a scheme-less issuer.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env.cas ]; then
  echo "ERROR: .env.cas not found. Run: cp .env.cas.example .env.cas" >&2
  exit 1
fi
# shellcheck disable=SC1091
source .env.cas

: "${STYTCH_DOMAIN:?set STYTCH_DOMAIN in .env.cas (full https URL of your custom domain)}"
: "${MCP_AUDIENCE:?set MCP_AUDIENCE in .env.cas}"
: "${MCP_BASE_URL:?set MCP_BASE_URL in .env.cas}"

DOMAIN="${STYTCH_DOMAIN%/}"   # strip trailing slash

# --- Stytch Connected Apps -> MCP resource-server settings ------------------
export CB_MCP_OAUTH_JWT_JWKS_URI="${DOMAIN}/.well-known/jwks.json"
export CB_MCP_OAUTH_JWT_ISSUER="${DOMAIN}"          # real https URL via custom domain
export CB_MCP_OAUTH_JWT_AUDIENCE="${MCP_AUDIENCE}"
# RS256 is the default algorithm; Stytch signs with RS256.
# PRM ON: advertise Stytch (the issuer) as the authorization server.
export CB_MCP_OAUTH_MCP_BASE_URL="${MCP_BASE_URL%/}"

# --- Couchbase cluster ------------------------------------------------------
CONNECTION_STRING="${CB_CONNECTION_STRING:-couchbases://localhost}"
USERNAME="${CB_USERNAME:-Administrator}"
PASSWORD="${CB_PASSWORD:-password}"

echo "JWKS:     $CB_MCP_OAUTH_JWT_JWKS_URI"
echo "Issuer:   $CB_MCP_OAUTH_JWT_ISSUER"
echo "Audience: $CB_MCP_OAUTH_JWT_AUDIENCE"
echo "PRM at:   ${MCP_BASE_URL%/}/.well-known/oauth-protected-resource/mcp"
echo

# Run from the repo root so uv resolves the project.
cd ../..
exec uv run mcp-server \
  --transport=http \
  --connection-string="$CONNECTION_STRING" \
  --username="$USERNAME" \
  --password="$PASSWORD"
