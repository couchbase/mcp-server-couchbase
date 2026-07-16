# Stytch OAuth testing for the Couchbase MCP server

A self-contained sandbox for exercising the Couchbase MCP server's OAuth paths
against **Stytch**. Fully isolated from the main project — nothing here is
imported by `src/`, and it can be deleted without touching the server.

The Couchbase MCP server is a pure **OAuth 2.0 resource server**
([`src/cb_mcp/auth.py`](../src/cb_mcp/auth.py)): it only validates bearer JWTs
against a JWKS. That's why one server works for both no-UI and interactive
flows — only the *client* and *token-minting* differ.

## Which folder do I want?

| Folder | Flow | UI? | Use it to test |
|---|---|---|---|
| [`m2m/`](m2m/README.md) | `client_credentials` (machine-to-machine) | **No** | Token verification + per-tool scope matrix, fully scripted. The direct analog of the repo's [`mint.sh`](../mint.sh) / [`cognito-mint.sh`](../cognito-mint.sh). |
| [`connected-apps/`](connected-apps/README.md) | Authorization code + PKCE | **Yes** (Next.js) | Interactive **DCR** and **non-DCR** browser login + consent, driven by MCP Inspector, against Stytch Connected Apps. |

## Answering the original questions

- **Can we test OAuth with Stytch without any UI?** Yes — use **M2M**
  (`m2m/`). No browser, no consent screen; you `curl` a `client_id` +
  `client_secret` for a signed JWT, exactly like the Auth0/Cognito minters.

- **How do I configure Stytch?** Two layers, both documented in the subfolder
  READMEs:
  - *Resource server (always):* point the three `CB_MCP_OAUTH_JWT_*` settings
    at your Stytch project's JWKS / issuer / audience.
  - *Authorization server (interactive only):* enable Connected Apps, define
    the `couchbase-mcp:read` / `couchbase-mcp:write` scopes, set the
    Authorization URL to the frontend's `/oauth/authorize`, and — for the
    native-PRM topology — configure a **custom domain** so the issuer is a real
    URL.

## Key Stytch facts (verified against Stytch docs)

| | Default (M2M & no custom domain) | With custom domain (Connected Apps) |
|---|---|---|
| `iss` | `stytch.com/<project_id>` (no scheme) | `https://auth.you.com` |
| `aud` | `<project_id>` | `<project_id>` (verify) |
| `scope` claim | space-delimited string (FastMCP reads it directly) | same |
| Algorithm | RS256 | RS256 |
| JWKS | `https://<env>.stytch.com/v1/public/<project_id>/.well-known/jwks.json` | `https://auth.you.com/.well-known/jwks.json` |
| Server PRM (`CB_MCP_OAUTH_MCP_BASE_URL`) | **off** (scheme-less issuer fails the URL check) | **on** (issuer is a real URL) |

See [`OAUTH.md`](../OAUTH.md) for the server-side activation contract and the
three-layer permission model.
