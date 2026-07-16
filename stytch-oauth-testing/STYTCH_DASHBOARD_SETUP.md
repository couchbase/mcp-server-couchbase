# Stytch Dashboard Setup — Consumer (B2C) project for Couchbase MCP OAuth

A complete, click-by-click guide to configuring a **Consumer (B2C)** Stytch
project to exercise all three OAuth flows against the Couchbase MCP server:

- **Flow A — M2M / token verification** (no UI): `client_credentials`.
- **Flow B — Non-DCR** (UI): pre-registered first-party Connected App + auth code + PKCE.
- **Flow C — DCR** (UI): Dynamic Client Registration + auth code + PKCE.

> ⚠️ **Substitute your real values everywhere.** This guide uses placeholders
> like `<project_id>`, `<custom-domain>`, and `auth.example.com`. Copying a
> placeholder literally is the #1 cause of the failures we hit — always use
> **your** project id and **your** custom domain (e.g.
> `marble-fastball-9645.customers.stytch.dev`).
>
> Sidebar labels shift between dashboard versions; if a section name doesn't
> match exactly, use the dashboard search.

---

## 0. Create the project

1. Sign up / log in at <https://stytch.com>.
2. Create a new project → choose **Consumer Authentication** (B2C), **not** B2B.
   (The frontend UI in this repo uses the Consumer SDK; B2B needs an Organization
   and a different SDK.)
3. Work in the **Test** environment for all of this.

## 1. Grab your keys

Dashboard → **Project settings → API keys**. Note:

| Value | Looks like | Used for |
|---|---|---|
| **Project ID** | `project-test-…` | `aud` claim, M2M URLs, token-only issuer |
| **Secret** | `secret-test-…` | Management API (creating M2M clients), M2M mgmt |
| **Public token** | `public-token-test-…` | Frontend SDK (`NEXT_PUBLIC_STYTCH_PUBLIC_TOKEN`) |

## 2. Define the OAuth scopes (shared by all flows)

The server enforces two canonical scopes: `couchbase-mcp:read`,
`couchbase-mcp:write`. Define them once so tokens can carry them and the consent
screen can show them.

- **Connected Apps → Scopes** → add:
  - `couchbase-mcp:read`
  - `couchbase-mcp:write`

Stytch allows `:` in scope names, so use the canonical form verbatim — no alias
mapping is needed on the server. (For M2M clients you'll assign the same two
scope strings when you create each client, step 3.)

---

## 3. Flow A — M2M (token verification, no UI)

The simplest flow. No custom domain, no UI, no Connected App.

### Dashboard
- Go to **M2M Clients** (under **Management** / **Connected Apps → M2M**).
- Create four clients and assign scopes to each:

  | Client name | Scopes |
  |---|---|
  | `couchbase-mcp-test-read`  | `couchbase-mcp:read` |
  | `couchbase-mcp-test-write` | `couchbase-mcp:write` |
  | `couchbase-mcp-test-both`  | `couchbase-mcp:read`, `couchbase-mcp:write` |
  | `couchbase-mcp-test-none`  | *(none)* |

  Copy each **Client ID** and **Client Secret** (the secret is shown **once**).

  > Faster, no clicks: `stytch-oauth-testing/m2m/setup-clients.sh` creates all
  > four via the management API. See [`m2m/README.md`](m2m/README.md).

### Server config (token-only — issuer is scheme-less, so **no** PRM)
```
--oauth-jwks-uri  https://test.stytch.com/v1/public/<project_id>/.well-known/jwks.json
--oauth-issuer    stytch.com/<project_id>          # NB: no https:// scheme
--oauth-audience  <project_id>
# do NOT set --oauth-mcp-base-url  (PRM needs a URL issuer; M2M's is scheme-less)
```

### Test
`m2m/stytch-mint.sh read|write|both|none` → `curl` `/mcp` with the token. Verifies
the scope matrix without any browser.

---

## 4. Custom domain (required for Flow C, recommended for Flow B)

By default Stytch's token issuer is scheme-less (`stytch.com/<project_id>`),
which the server's PRM mode rejects — and the DCR metadata (`registration_endpoint`)
is only served on a custom domain. So DCR needs one.

- **Project details → Custom Domains → “+ Add New”**.
- Stytch shows a target like `{SUBDOMAIN}.stytch.com` (or a `*.customers.stytch.dev`
  host). Add a **CNAME** at your DNS provider: `auth.example.com` → that target.
- Enter `auth.example.com` in the dialog → **Verify**. Live in minutes (≤48h).

**Confirm it serves DCR metadata** before continuing:
```bash
curl -s https://auth.example.com/.well-known/oauth-authorization-server \
  | jq '{issuer, authorization_endpoint, token_endpoint, registration_endpoint, jwks_uri}'
```
You must see `issuer: "https://auth.example.com"` and a `registration_endpoint`.
After a custom domain, the endpoints are:

| Endpoint | URL |
|---|---|
| Issuer (`iss`) | `https://auth.example.com` |
| AS metadata | `https://auth.example.com/.well-known/oauth-authorization-server` |
| JWKS | `https://auth.example.com/.well-known/jwks.json` |
| Token | `https://auth.example.com/v1/oauth2/token` |
| Register (DCR) | `https://auth.example.com/v1/oauth2/register` |

---

## 5. Connected Apps config (Flows B & C)

Dashboard → **Connected Apps**:

1. **Authorization URL** = `http://localhost:3000/oauth/authorize`
   - This is the **frontend** consent route (`<IdentityProvider/>`), NOT the MCP
     server. Full path with `/oauth/` — not bare `/authorize`, not `:8000`.
   - This value becomes `authorization_endpoint` in the AS metadata.
2. **Enable Dynamic Client Registration** → **ON** (required for Flow C).
3. Scopes `couchbase-mcp:read` / `couchbase-mcp:write` defined (step 2). ✓

### Flow B only — create a first-party (pre-registered) client
- **Connected Apps → “+ Create”** a first-party app.
- Note its **Client ID** (and **Client Secret** if you make it confidential).
- Add its **Redirect URLs** (the OAuth client callbacks) — e.g.
  `https://vscode.dev/redirect` and/or `http://127.0.0.1:<port>/`.
  - ⚠️ These OAuth `redirect_uri`s go **here**, on the Connected App — **not** in
    the Frontend SDK Login/Logout URLs (step 6). Different mechanism.
  - Prefer `https://vscode.dev/redirect` since loopback ports vary per session.
- You feed this Client ID to your MCP client to **skip DCR**.

> Flow C (DCR) needs no manual client — the client self-registers its
> `redirect_uri` at runtime via `/v1/oauth2/register`.

## 6. Frontend SDK config (Flows B & C — for the login/consent UI)

Dashboard → **Frontend SDKs**:

1. **Enable the SDK** and add **Authorized applications / domains**:
   `http://localhost:3000`.
2. **Enable a login method** — **Email magic links / OTP** (Consumer default).
   The UI in this repo uses email one-time passcodes.
3. **Redirect URLs** (these are your *app's* URLs, where Stytch returns the user
   after login/logout — NOT the OAuth client callbacks):
   - **Login**: `http://localhost:3000`
   - **Logout**: `http://localhost:3000`
   - (`http://localhost:3000/authenticate` only if you enable Google OAuth.)

---

## 7. Per-flow cheat sheet (server flags + client)

Replace `<project_id>` and `auth.example.com` with your real values.

| | Flow A — M2M | Flow B — Non-DCR | Flow C — DCR |
|---|---|---|---|
| UI needed | No | Yes (`:3000`) | Yes (`:3000`) |
| Custom domain | No | Recommended | **Required** |
| `--oauth-jwks-uri` | `…/v1/public/<project_id>/.well-known/jwks.json` | `https://auth.example.com/.well-known/jwks.json` | same |
| `--oauth-issuer` | `stytch.com/<project_id>` | `https://auth.example.com` | `https://auth.example.com` |
| `--oauth-audience` | `<project_id>` | `<project_id>` | `<project_id>` |
| `--oauth-mcp-base-url` | *(unset)* | `http://127.0.0.1:8000` | `http://127.0.0.1:8000` |
| Client | `stytch-mint.sh` + curl | MCP client w/ pinned Client ID | MCP Inspector / VS Code (auto-registers) |

**Consistency rule:** use **one host** everywhere. If `--host 127.0.0.1` and
`--oauth-mcp-base-url http://127.0.0.1:8000`, then your MCP client must connect to
`http://127.0.0.1:8000/mcp` — never mix `localhost` and `127.0.0.1`.

## 8. Verify + troubleshoot

Boot the server (Flow B/C) — expect:
```
OAuth enabled with PRM at http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp
```
Confirm PRM points at your real domain:
```bash
curl -s http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp | jq '{resource, authorization_servers}'
# authorization_servers → ["https://auth.example.com"]   (NOT localhost:8000, NOT a placeholder)
```
Decode a token you obtain and align config:
```bash
echo '<jwt>' | cut -d. -f2 | tr '_-' '/+' \
  | { read p; case $((${#p}%4)) in 2) p="$p==";; 3) p="$p=";; esac; echo "$p"; } \
  | base64 -d | jq '{iss, aud, scope}'
```

| Symptom | Cause / fix |
|---|---|
| Boot: `issuer … must be a valid http(s) URL` | Flow B/C needs the **custom-domain** issuer (`https://…`), not the scheme-less M2M one. |
| Client does DCR against `:8000/register` → 404, asks for client id/secret | PRM `authorization_servers` isn't your real domain — issuer is a placeholder or wrong. Fix `--oauth-issuer`. |
| `Could not fetch resource metadata` / falls back to `localhost:8000` | `localhost` vs `127.0.0.1` mismatch between client URL and `--oauth-mcp-base-url`. Make them identical. |
| Consent screen 404 / "Not Found" on redirect | **Authorization URL** ≠ your running UI. Set it to `http://localhost:3000/oauth/authorize` and run the frontend. |
| After login, no redirect back to the client | Frontend bug (fixed in this repo): the home page must redirect to the stored `returnTo` on session. |
| `audience mismatch (got ['project-test-…'])` | Stytch stamps `aud` = **project id** (ignores the `resource` param). Set `--oauth-audience <project_id>`. |
| Authorize rejected: unknown scope | Define `couchbase-mcp:read` / `:write` in Connected Apps → Scopes. |

---

## Where the flows live in this repo
- Flow A: [`m2m/`](m2m/README.md)
- Flows B & C: [`connected-apps/`](connected-apps/README.md) (frontend UI + server launcher)
