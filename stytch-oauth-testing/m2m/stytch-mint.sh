#!/usr/bin/env bash
# Stytch M2M token minter for Couchbase MCP testing (no UI).
#
# Exchanges a client_credentials grant for a signed JWT at:
#   POST https://<env>.stytch.com/v1/public/<PROJECT_ID>/oauth2/token
#
# Each client is already scoped (see setup-clients.sh), so we omit the `scope`
# param and Stytch returns that client's full assigned scope set.
#
# Usage:
#   ./stytch-mint.sh read            # only couchbase-mcp:read
#   ./stytch-mint.sh write           # only couchbase-mcp:write
#   ./stytch-mint.sh both            # both scopes
#   ./stytch-mint.sh none            # no scopes (every tool denied)
#   ./stytch-mint.sh both --decode   # also pretty-print the JWT claims

set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-}"
DECODE="${2:-}"

case "$MODE" in
  read|write|both|none) ;;
  *) echo "usage: $0 read|write|both|none [--decode]" >&2; exit 1 ;;
esac

if [ ! -f .env.stytch ]; then
  echo "ERROR: .env.stytch not found. Run ./setup-clients.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1091
source .env.stytch

: "${STYTCH_ENV:?set STYTCH_ENV in .env.stytch}"
: "${STYTCH_PROJECT_ID:?set STYTCH_PROJECT_ID in .env.stytch}"

PREFIX="$(echo "$MODE" | tr '[:lower:]' '[:upper:]')"
CID_VAR="${PREFIX}_CLIENT_ID"
CSEC_VAR="${PREFIX}_CLIENT_SECRET"
CID="${!CID_VAR:-}"
CSEC="${!CSEC_VAR:-}"

if [ -z "$CID" ] || [ -z "$CSEC" ]; then
  echo "ERROR: ${CID_VAR}/${CSEC_VAR} missing in .env.stytch. Run ./setup-clients.sh." >&2
  exit 1
fi

TOKEN_URL="https://${STYTCH_ENV}.stytch.com/v1/public/${STYTCH_PROJECT_ID}/oauth2/token"

RESP=$(curl -sS -X POST "$TOKEN_URL" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "grant_type=client_credentials" \
  --data-urlencode "client_id=${CID}" \
  --data-urlencode "client_secret=${CSEC}")

# Surface Stytch OAuth errors instead of silently emitting an empty token.
if echo "$RESP" | jq -e '.error? // .error_type? // empty' >/dev/null 2>&1; then
  echo "Stytch returned an error:" >&2
  echo "$RESP" | jq . >&2
  exit 1
fi

TOKEN=$(echo "$RESP" | jq -r '.access_token // empty')
if [ -z "$TOKEN" ]; then
  echo "No access_token in response:" >&2
  echo "$RESP" | jq . >&2
  exit 1
fi

if [ "$DECODE" = "--decode" ]; then
  # base64url-decode the JWT payload (2nd segment) and pretty-print claims.
  PAYLOAD=$(echo "$TOKEN" | cut -d. -f2 | tr '_-' '/+')
  case $(( ${#PAYLOAD} % 4 )) in 2) PAYLOAD="${PAYLOAD}==";; 3) PAYLOAD="${PAYLOAD}=";; esac
  echo "$PAYLOAD" | base64 -d 2>/dev/null | jq . >&2
fi

echo "$TOKEN"
