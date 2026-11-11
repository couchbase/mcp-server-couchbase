# Origin allow-list (`--allowed-origins`)

This page explains what the origin allow-list does, why it exists, and how it
is built. For the user-facing settings reference, see the "Restricting browser
origins" section of the [README](../../README.md#restricting-browser-origins).

## The problem

When the server runs over HTTP, any web page open in your browser can try to
send requests to it, including to a server on `localhost` or inside your
company network. A page cannot read your files, but it can make your browser
call the MCP endpoint on its behalf. A trick called DNS rebinding makes this
work even for servers that only listen on `127.0.0.1`.

## The idea

Every request a browser sends carries an `Origin` header, such as
`https://evil.example`. It names the website that made the request. Page
JavaScript cannot change or hide it, so a server can safely refuse origins it
does not know.

`--allowed-origins` lets the operator list the origins that may call the
server. Everything else gets `403 Forbidden Origin`.

```bash
couchbase-mcp-server operational --transport=http \
  --allowed-origins='https://app.example.com,http://localhost:3000,https://*.example.org'
```

The same value can come from the `CB_MCP_ALLOWED_ORIGINS` environment variable.

## The rules

| Situation | Result |
| --- | --- |
| Option unset, empty, only commas, or a lone `*` | No check. Every request is admitted, with or without `Origin`. |
| List set, request has a matching `Origin` | Admitted. |
| List set, request has a different `Origin` | `403`. |
| List set, request has **no** `Origin` | `403`. |
| `Origin: null` | Always `403`. It cannot be listed. |
| `GET /health` | Always admitted. |
| Origin is on the list | The reply also carries CORS headers, so the page can read it (see [CORS](#cors-letting-listed-pages-read-the-reply)). |
| `stdio` transport | The option is ignored (a warning is logged). |

The one that surprises people is the fourth row. Browsers always send `Origin`
on the requests that matter, but most non-browser clients (desktop apps, CLI
agents) send none. Once a list is set those clients are turned away, so set it
only when browsers are what you need to admit or defend against.

The `Host` header is never looked at. Protecting it is a possible follow-up.

### What an entry looks like

An entry is `scheme://host[:port]`:

- Any scheme works: `https://`, `chrome-extension://<id>`, `vscode-webview://<id>`.
- Default ports are optional: `https://a.com` and `https://a.com:443` are the same entry.
- A `*` in the **host** matches any run of hostname characters (letters, digits, `.`, `-`, `_`): `https://*.example.com`. It is not allowed in the scheme or port, and it never reaches into the port, so `https://app.example.*` admits `https://app.example.com` but not `https://app.example.com:8443`.
- **Internationalized hostnames are rejected, not converted.** Browsers send them in punycode, so `https://bücher.example` could never match what a browser sends (`https://xn--bcher-kva.example`). Rather than accept an entry that silently admits nobody, startup fails and the error shows the form to list. Converting automatically would not work for wildcards, because `*` is not valid in IDNA.
- Matching ignores case.

A lone `*` means "allow everything", but combining it with other entries is an
error, because the intent would be unclear.

## CORS: letting listed pages read the reply

Browsers have a second rule, separate from the allow-list. A page may *send* a
request to another origin, but the browser hides the reply from the page unless
the server says that origin may read it. That permission is called CORS
(Cross-Origin Resource Sharing), and only the server being called can grant it,
by adding a header to its reply.

| | Allow-list | CORS |
| --- | --- | --- |
| Who enforces it | The server | The browser |
| What it decides | Whether the server handles the request at all | Whether the page may read the reply |
| Without it | Unknown pages can call the server | Listed pages can call it but cannot read the answer |

Without CORS the allow-list would admit a listed page and the browser would
still throw the answer away. So the server also sends CORS headers, **only to
origins on the list**:

- MCP calls are `POST`s with `Content-Type: application/json`, so the browser
  first sends a preflight `OPTIONS` request asking "may this origin send that?".
  The server answers it with `Access-Control-Allow-Origin: <the origin>`.
- Replies to listed origins carry the same header. Two response headers are
  exposed, because browsers hide all others from page code: `Mcp-Session-Id`
  (an MCP client reads it from the `initialize` reply) and `WWW-Authenticate`
  (a 401 challenge carries the URL a browser OAuth client uses to discover the
  authorization server).
- An origin that is not listed gets no CORS headers, and its request is
  refused with `403` anyway.
- With no list set, no CORS headers are sent and nothing changes.

```
page (https://app.example.com)                 MCP server
   | -- OPTIONS  Origin: https://app.example.com -->  |  preflight
   | <- 200  Access-Control-Allow-Origin: ... --------|  listed: allowed
   | -- POST /mcp  Origin: https://app.example.com -->|  the real call
   | <- 200 + Access-Control-Allow-Origin: ... -------|  page can read it
```

CORS only affects browsers. A script can ignore it, which is one more reason
the allow-list is not authentication.

## What this does not do

It is a guard against **web pages**, not against other programs. Any
non-browser client can send whatever `Origin` it likes. It is not
authentication. Use OAuth for that.

## Bad lists stop the server

A security setting must not quietly turn into "allow everything" because of a
typo, so a malformed list makes startup fail with exit code 2. The error names
every bad entry and why:

```
Error: Invalid value for '--allowed-origins': invalid origin list:
  - 'example.com': missing scheme; start with something like https://
  - 'https://x.com/p': contains a path (/p); an origin is only scheme://host[:port]
Each entry must be an origin: scheme://host[:port], for example ...
(set by --allowed-origins or CB_MCP_ALLOWED_ORIGINS)
```

This also happens in the `--workers` supervisor before any worker is spawned.

## How it is built

The feature has three parts, all in `src/cb_mcp/core/origin_guard.py` except
the wiring.

```
CLI flag / env var
      |
      v
validate_allowed_origins   (utils/cli.py)        fails startup on a bad list
      |
      v
parse_allowed_origins      (origin_guard.py)     text -> normalized tuple
      |
      v
TransportParams.allowed_origins                  (utils/cli_params.py)
      |
      v
origin_middleware(...)     (origin_guard.py)     -> run_app / create_app
      |
      v
CORSMiddleware  ->  OriginAllowlistMiddleware    CORS headers, then the check
```

1. **Parsing** (`parse_allowed_origins`). Splits the comma list, normalizes each
   entry (lowercase, default port dropped) using `urllib.parse.urlsplit` plus a
   few extra checks, and collects *all* problems into one `OriginListError`.
   An empty result means "nothing to enforce".

2. **Enforcement** (`OriginAllowlistMiddleware`). A small ASGI middleware. The
   heart of it:

   ```python
   origin = _origin_header(scope)
   if origin is not None and self._allows(origin):
       await self.app(scope, receive, send)      # let it through
       return
   ...
   await PlainTextResponse("Forbidden Origin", status_code=403)(scope, receive, send)
   ```

   Entries without `*` are matched from a set; entries with `*` are turned into
   regular expressions. It does not use `fnmatch` because `[` and `]` in an IPv6
   origin such as `http://[::1]:3000` would be read as a character class.

   In front of it sits Starlette's `CORSMiddleware`, built from the same list
   (`origin_middleware`), so preflights are answered before the guard runs:

   ```python
   return [
       Middleware(CORSMiddleware, allow_origin_regex=any_listed, ...),
       Middleware(OriginAllowlistMiddleware, allowed_origins=origins),
   ]
   ```

3. **Wiring.** `run_app` (`core/app.py`) passes the middleware to FastMCP for
   `http` and `sse`. With `--workers`, `create_app` in `src/mcp_server.py` does
   the same in each worker. With no list, no middleware is added at all.

The value is also shown in the settings that the server reports
(`allowed_origins` in `get_server_configuration_status` and the startup
diagnostic).

### Why not FastMCP's built-in check?

FastMCP has a Host/Origin guard, but it admits requests that have no `Origin`
and falls back to comparing against the `Host` header, which an attacker
controls in a rebinding attack. That is the opposite of the strict rule wanted
here, so a small custom middleware is used instead.

## Where the tests are

- `tests/unit/test_origin_list_parsing.py`: the parser and its error messages.
- `tests/unit/test_origin_allowlist.py`: end-to-end behaviour through the real
  CLI, for single-process, `--workers` and SSE.
