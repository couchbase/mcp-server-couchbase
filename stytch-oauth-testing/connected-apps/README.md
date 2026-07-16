# Stytch Connected Apps — interactive OAuth (DCR + non-DCR)

Exercises the **full interactive** OAuth surface of the Couchbase MCP server
against **Stytch Connected Apps** (its OAuth 2.1 authorization server):

- **Token verification** — the server validates the JWTs Stytch issues.
- **DCR** — an MCP client (MCP Inspector) discovers the server, **dynamically
  registers** itself, then runs browser login + consent.
- **Non-DCR** — the same auth-code + PKCE flow using a **pre-registered**
  first-party Connected App `client_id`.

This needs a real login/consent **UI** — that's the Next.js app in
[`frontend/`](frontend/). Everything is isolated from the main repo.

> For the no-UI machine-to-machine path (also "token verification"), see
> [`../m2m/`](../m2m/README.md).

---

## Topology (chosen: custom domain + native PRM)

```
  ┌──────────────┐  1. connect /mcp        ┌───────────────────────────┐
  │ MCP Inspector │ ───────────────────────▶│ Couchbase MCP server      │
  │  (browser)   │  ◀── 401 + PRM pointer   │  :8000  (Python, this repo)│
  └──────┬───────┘                          │  resource server + PRM    │
         │ 2. read PRM → authorization_servers = https://auth.you.com
         │ 3. read https://auth.you.com/.well-known/oauth-authorization-server
         ▼                                   └───────────────────────────┘
  ┌───────────────────────────┐  4. (DCR) POST /v1/oauth2/register
  │ Stytch (custom domain)    │◀──────────────────────────────────────
  │  https://auth.you.com     │  5. token, jwks, register endpoints
  │  authz server + JWKS      │  authorization_endpoint ─┐
  └───────────────────────────┘                          │
         ▲ 7. consent → code → token                     │ 6. browser login
         │                                                ▼
  ┌───────────────────────────┐                  ┌──────────────────────┐
  │ Stytch hosts /v1/oauth2/*  │                  │ frontend  :3000      │
  │ (token, register, jwks)    │                  │  /            login  │
  └───────────────────────────┘                  │  /oauth/authorize    │
                                                  │      <IdentityProvider/> consent
                                                  └──────────────────────┘
```

**Why a custom domain?** Stytch's default token issuer is the scheme-less
`stytch.com/<project_id>`. The MCP server publishes its `iss` as the
authorization server in PRM and requires a valid `https://` URL
([mcp_server.py:439-448](../../src/mcp_server.py#L439-L448)). A custom auth
domain makes `iss = https://auth.you.com`, so the server's built-in PRM mode
works end-to-end with zero code changes.

The **custom domain is a CNAME to Stytch** — it serves Stytch's
token/register/jwks/metadata endpoints. Your frontend (login + consent) and the
MCP server stay on localhost.

---

## One-time Stytch setup (dashboard)

1. **Create a Consumer project** (or reuse one) at <https://stytch.com>.

2. **Configure a custom auth domain.** Dashboard → *Configuration → Domains*
   (a.k.a. custom domains). Add e.g. `auth.yourcompany.com` and create the
   CNAME it tells you to. Confirm tokens then carry `iss: https://auth.yourcompany.com`.
   Docs: <https://stytch.com/docs/resources/branding/custom-domains>

3. **Connected Apps → define scopes.** Dashboard → *Connected Apps*. Add two
   scopes so clients can request them and the consent screen can show them:
   - `couchbase-mcp:read`
   - `couchbase-mcp:write`
   (These are the server's canonical scopes — no aliasing needed.)

4. **Set the Authorization URL** to where the consent component is mounted:
   `http://localhost:3000/oauth/authorize`

5. **Enable Dynamic Client Registration** (for the DCR flow). Same Connected
   Apps screen — toggle DCR / third-party clients on.

6. **(Non-DCR only) Create a first-party Connected App** and note its
   **Client ID** (and secret if you make it confidential). You'll feed this
   `client_id` to MCP Inspector to skip DCR.

7. Grab from *Project settings → API keys*: **public_token**, **project_id**.

---

## Run it (three processes)

### A. Frontend (login + consent UI) — port 3000

```bash
cd frontend
cp .env.template .env.local        # fill NEXT_PUBLIC_STYTCH_PUBLIC_TOKEN, STYTCH_DOMAIN, project id
npm install
npm run dev                        # http://localhost:3000
```

Open <http://localhost:3000> and sign in once (email OTP) to confirm login works.

### B. Couchbase MCP server (resource server + PRM) — port 8000

```bash
# from this connected-apps/ dir
cp .env.cas.example .env.cas       # fill STYTCH_DOMAIN, MCP_AUDIENCE, Couchbase creds
chmod +x run-mcp-server.sh
./run-mcp-server.sh
```

You should see the boot log:

```
OAuth enabled with PRM at http://localhost:8000/.well-known/oauth-protected-resource/mcp
(scopes=['couchbase-mcp:read', 'couchbase-mcp:write'])
```

Sanity-check the PRM document:

```bash
curl -s http://localhost:8000/.well-known/oauth-protected-resource/mcp | jq .
# authorization_servers should list your custom domain.
```

### C. MCP Inspector (the test client)

```bash
npx @modelcontextprotocol/inspector
```

---

## Flow 1 — DCR (dynamic client registration)

1. In MCP Inspector: Transport = **Streamable HTTP**, URL =
   `http://localhost:8000/mcp`. Connect.
2. Inspector gets a 401, reads the server's PRM, follows it to your custom
   domain's `/.well-known/oauth-authorization-server`, and **auto-registers**
   at `https://auth.you.com/v1/oauth2/register` (no client_id needed).
3. Inspector opens `http://localhost:3000/oauth/authorize`. If you're not
   logged in, the frontend bounces you to `/` to sign in, then back.
4. Stytch's `<IdentityProvider/>` renders the **consent screen** showing
   `couchbase-mcp:read` / `couchbase-mcp:write`. Approve.
5. Stytch issues a code → Inspector exchanges it at the token endpoint →
   gets a JWT → calls tools. List tools and invoke `get_buckets_in_cluster`.

## Flow 2 — non-DCR (pre-registered first-party client)

Same as above, but in Inspector's **Authentication / OAuth settings** enter the
**Client ID** of the first-party Connected App from setup step 6 (and secret if
confidential). Inspector then **skips** the `/oauth2/register` call and uses
your static client straight into the authorize + token exchange.

> If your Inspector build can't pin a client_id, drive it manually: build an
> authorize URL against `https://auth.you.com/v1/oauth2/authorize?...` with
> PKCE, log in via the frontend, capture the `code` on your redirect URI, and
> `POST https://auth.you.com/v1/oauth2/token`. Same endpoints, no DCR.

## Flow 3 — token verification

Both flows above already prove verification: every tool call carries a Stytch
JWT that the server validates against the custom-domain JWKS. The
read/write/both scope behavior follows the matrix in
[`OAUTH.md`](../../OAUTH.md#requirement-3--built-in-scopes) — request only
`couchbase-mcp:read` and confirm write tools return `PermissionError`.

---

## Verify the token (audience gotcha)

The single most likely misconfig is **audience**. Decode the JWT Inspector
obtained (copy it from Inspector, or from the server's debug logs):

```bash
# paste the JWT:
echo '<paste.jwt.here>' | cut -d. -f2 | tr '_-' '/+' \
  | { read p; case $((${#p}%4)) in 2) p="$p==";; 3) p="$p=";; esac; echo "$p"; } \
  | base64 -d 2>/dev/null | jq '{iss, aud, scope}'
```

- `iss` must equal `STYTCH_DOMAIN` (your custom domain URL).
- `aud` must equal `MCP_AUDIENCE` in `.env.cas`. Stytch defaults `aud` to the
  **project_id**; if your client sends RFC 8707 `resource` and Stytch reflects
  it, set `MCP_AUDIENCE` to that value instead and restart the server.
- `scope` should contain `couchbase-mcp:read` and/or `couchbase-mcp:write`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Server boot: `--oauth-issuer ... must be a valid http(s) URL` | `STYTCH_DOMAIN` isn't set to your custom-domain **https URL**, or you're still on the default scheme-less issuer. Custom domain is required for PRM mode. |
| Inspector can't discover the auth server | PRM `authorization_servers` must resolve to a real `.well-known/oauth-authorization-server`. Confirm the custom domain is live and serves metadata; `curl https://auth.you.com/.well-known/oauth-authorization-server`. |
| Consent screen 404 / blank | Authorization URL in Stytch must be exactly `http://localhost:3000/oauth/authorize` and the frontend (`npm run dev`) must be running. |
| Authorize rejected: unknown scope | Define `couchbase-mcp:read` / `couchbase-mcp:write` as Connected Apps scopes (setup step 3). |
| Tool call: `401 invalid token` | `aud`/`iss` mismatch — decode the token (above) and align `.env.cas`. |
| Tool call: `PermissionError: missing [...]` | Expected when the granted scope doesn't cover the tool — that's the matrix working. |
| DCR works but non-DCR doesn't | The first-party client's redirect URI must match what Inspector uses; confirm it in the Connected App config. |

---

## Files

| Path | Purpose |
|---|---|
| `frontend/` | Next.js login + `/oauth/authorize` consent UI (Stytch React SDK). |
| `frontend/src/components/Auth.tsx` | Login config + `IdentityProvider = withLoginRequired(...)`. |
| `frontend/src/app/oauth/authorize/page.tsx` | The consent route you register as the Authorization URL. |
| `frontend/src/app/.well-known/.../route.ts` | Optional AS-metadata fallback (not used on the custom-domain path). |
| `run-mcp-server.sh` | Boots the Couchbase MCP server in **PRM mode** against your custom domain. |
| `.env.cas.example` | Template for the server-side config. |
