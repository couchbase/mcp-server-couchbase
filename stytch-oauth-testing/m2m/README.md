# Stytch OAuth testing (M2M / no UI)

Self-contained harness for exercising the Couchbase MCP server's OAuth
resource-server path against **Stytch**, with **no UI and no browser login**.

This folder is fully isolated from the rest of the repo — nothing here is
imported by `src/`. It is the Stytch equivalent of the existing root-level
minters [`mint.sh`](../../mint.sh) (Auth0) and [`cognito-mint.sh`](../../cognito-mint.sh)
(AWS Cognito).

---

## TL;DR — can we do this without a UI?

**Yes.** Stytch's interactive products (Connected Apps, Consumer/B2B login)
need a browser for the **authorization-code** grant — user login + a consent
screen. But that whole flow exists to identify a *human*. The Couchbase MCP
server doesn't care: it's a pure **OAuth 2.0 resource server**
([`src/cb_mcp/auth.py`](../../src/cb_mcp/auth.py)) — it only validates that the
bearer JWT is signed by the configured JWKS and carries the right
`iss` / `aud` / `scope` claims. It never sees the login.

So we use Stytch **M2M (machine-to-machine) clients** and the
**`client_credentials`** grant: exchange a `client_id` + `client_secret` for
a signed JWT over a single `curl`. No UI, no redirect, no PKCE — identical in
shape to what `mint.sh` and `cognito-mint.sh` already do.

> Connected Apps (the browser/DCR path) is the right choice when you want to
> demo a real MCP client (Claude, Cursor) doing interactive login + consent.
> It is **out of scope** here by design — see the bottom of this file.

---

## How Stytch M2M maps onto the MCP server's three required settings

The server enables OAuth only when all three JWT settings are present and
`--transport=http` (see [`OAUTH.md`](../../OAUTH.md)). Here is the exact mapping
for Stytch M2M — note how it differs from Auth0/Cognito:

| MCP server setting (`CB_MCP_OAUTH_JWT_*`) | Value for Stytch M2M | Notes |
|---|---|---|
| `..._JWKS_URI` | `https://test.stytch.com/v1/public/<PROJECT_ID>/.well-known/jwks.json` | `live.stytch.com` in production. Alt: `.../v1/sessions/jwks/<PROJECT_ID>`. |
| `..._ISSUER` | `stytch.com/<PROJECT_ID>` | ⚠️ **No `https://` scheme** — Stytch puts the bare host in `iss`. |
| `..._AUDIENCE` | `<PROJECT_ID>` | ⚠️ Fixed to the project ID. Unlike Auth0/Cognito, you **cannot** set a custom resource audience for M2M tokens. |
| `..._ALGORITHM` | `RS256` | This is already the server default — you can omit it. |
| `CB_MCP_OAUTH_MCP_BASE_URL` | **leave unset** | PRM mode requires a URL issuer (`AnyHttpUrl`). Stytch's `iss` has no scheme, so PRM would fail to boot. M2M doesn't need PRM. |

A Stytch M2M token decodes to exactly this shape (verified against the
[Get M2M token docs](https://stytch.com/docs/api/get-m2m-token)):

```json
{
  "sub": "m2m-client-test-...",
  "iss": "stytch.com/project-test-xxxx",
  "aud": ["project-test-xxxx"],
  "scope": "couchbase-mcp:read couchbase-mcp:write",
  "iat": ..., "nbf": ..., "exp": ...
}
```

`scope` is a **space-delimited string** — exactly what FastMCP's
`JWTVerifier._extract_scopes` reads, which our `CouchbaseJWTVerifier`
([`auth.py:93`](../../src/cb_mcp/auth.py#L93)) then normalizes per-tool.

### Scope names: use the canonical form

Stytch scopes accept `:` (its own examples use `read:users`). So define the
scopes in Stytch **exactly** as the server's canonical scopes:

- `couchbase-mcp:read`
- `couchbase-mcp:write`

Because these are already canonical ([`constants.py`](../../src/cb_mcp/utils/constants.py)),
**no scope-alias flags are needed** — unlike Cognito, which forces a
`couchbase-mcp/read` prefix. (If Stytch ever rejects the `:`, fall back to the
dashed `couchbase-mcp-read` / `couchbase-mcp-write`, which the server's
built-in alias map handles with no flags either.)

---

## One-time Stytch setup (all via API — no dashboard clicks required)

1. Create a free Stytch project (Consumer or B2B both expose M2M) at
   <https://stytch.com>. This is the only browser step, and only to grab two
   values from **Project settings → API keys**:
   - **Project ID** — looks like `project-test-0000...`
   - **Secret** — looks like `secret-test-0000...` (project management secret)

2. Put them in `.env.stytch` (copy the example):

   ```bash
   cp .env.stytch.example .env.stytch
   # edit STYTCH_PROJECT_ID and STYTCH_PROJECT_SECRET
   ```

3. Create the four M2M test clients (read / write / both / none) over the
   management API — **no UI**:

   ```bash
   ./setup-clients.sh
   ```

   This calls `POST /v1/m2m/clients` four times and appends the resulting
   `client_id` / `client_secret` pairs back into `.env.stytch`. The secret is
   shown by Stytch **only once at creation**, so the script captures it
   immediately.

---

## Minting tokens (no UI)

```bash
./stytch-mint.sh read     # token carrying only couchbase-mcp:read
./stytch-mint.sh write    # token carrying only couchbase-mcp:write
./stytch-mint.sh both     # token carrying both scopes
./stytch-mint.sh none     # token with no scopes (every tool denied)
./stytch-mint.sh both --decode   # also pretty-print the JWT claims
```

Each prints a raw `access_token` to stdout, so it pipes straight into a curl
header or an env var.

---

## Running the server against Stytch and testing the matrix

```bash
# 1. Start the MCP server pointed at Stytch (edit the Couchbase creds inside).
./run-mcp-server.sh

# 2. In another shell, drive the scope matrix.
TOKEN=$(./stytch-mint.sh read)

# A read tool — allowed with the read token:
curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"get_buckets_in_cluster","arguments":{}}}'

# A write tool with the read token — expect PermissionError (missing write scope):
curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"upsert_document_by_id","arguments":{}}}'
```

Expected outcomes follow the permission matrix in [`OAUTH.md`](../../OAUTH.md#requirement-3--built-in-scopes):

| Token (`stytch-mint.sh ...`) | Read tools | Write tools | `run_sql_plus_plus_query` |
|---|---|---|---|
| `read`  | ✅ | ❌ | ✅ |
| `write` | ❌ | ✅ | ❌ |
| `both`  | ✅ | ✅ | ✅ |
| `none`  | ❌ | ❌ | ❌ |

(Cluster RBAC is still the ultimate authority on actual data writes — see the
three-layer model in `OAUTH.md`.)

---

## Files

| File | Purpose |
|---|---|
| `setup-clients.sh` | Creates the 4 M2M clients + scopes via the Stytch management API (no UI). |
| `stytch-mint.sh` | Mints a `client_credentials` token for read/write/both/none. |
| `run-mcp-server.sh` | Boots the MCP server in http mode wired to your Stytch project. |
| `.env.stytch.example` | Template for project + client credentials. |
| `.env.stytch` | Your real creds (git-ignored — never commit). |

---

## Out of scope: the UI / Connected Apps path

If you later want to test the **interactive** MCP OAuth flow (a client doing
discovery → Dynamic Client Registration → browser login → consent → code
exchange), that's Stytch **Connected Apps**, and it *does* need a UI for the
login/consent screen. That path also expects the server to publish
PRM/authorization-server metadata, which conflicts with M2M's scheme-less
issuer. Keep it separate from this folder. Refs:
- <https://stytch.com/docs/guides/connected-apps/mcp-servers>
- <https://stytch.com/blog/mcp-oauth-dynamic-client-registration/>
