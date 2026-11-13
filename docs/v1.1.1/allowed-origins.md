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
server. A request that carries any other origin gets `403 Forbidden Origin`.

```bash
couchbase-mcp-server operational --transport=http \
  --allowed-origins='https://app.example.com,http://localhost:3000,https://*.example.org'
```

The same value can come from the `CB_MCP_ALLOWED_ORIGINS` environment variable.

The check is not ours: it is FastMCP's own Host/Origin guard. Our part is to
validate the list at startup (FastMCP does none, so a typo would just never
match) and to hand it over. That choice decides the rules below.

## The rules

| Situation | Result |
| --- | --- |
| Option unset, empty, only commas, or a lone `*` | No check. Every request is admitted. |
| List set, request has a listed `Origin` | Admitted. |
| List set, request has an unlisted `Origin` | `403`. |
| `Origin: null` | `403`. It cannot be listed. |
| List set, request has **no** `Origin` | Admitted. |
| `GET /health` with an unlisted `Origin` | `403`. Probes send none, so they pass. |
| `stdio` transport | The option is ignored (a warning is logged). |

The fifth row is the important one. FastMCP only judges an `Origin` that is
present. Browsers send `Origin` on the requests that matter, while desktop apps
and CLI agents send none, so they keep working once a list is set. The price is
that a caller can skip the check by leaving the header out. That is why this is
a guard against web pages, not authentication.

### What an entry looks like

An entry is `scheme://host[:port]`:

- Any scheme works: `https://`, `chrome-extension://<id>`, `vscode-webview://<id>`.
- Default ports are optional: `https://a.com` and `https://a.com:443` are the same entry.
- A `*` in the **host** matches any text: `https://*.example.com`. It is not allowed in the scheme or port. Because the default port is filled in before matching, a trailing wildcard such as `https://app.example.*` admits `https://app.example.com` but not `https://app.example.com:8443`.
- Matching ignores case.

A lone `*` means "allow everything", but combining it with other entries is an
error, because the intent would be unclear.

### Two kinds of entry are rejected at startup

An entry that can never match is worse than an error: it looks like protection
and admits nobody. So startup fails on these, with the reason and what to do:

- **Internationalized hostnames.** Browsers send them in punycode, so
  `https://bücher.example` could never match what a browser sends
  (`https://xn--bcher-kva.example`). List the `xn--` form; the error prints it.
  We reject rather than convert because `*` is not valid in IDNA, so a
  conversion could not cover wildcards.
- **IPv6 addresses**, such as `http://[::1]:3000`. FastMCP compares origins with
  `fnmatch`, which reads the brackets as a pattern, so these never match. List a
  hostname that resolves to the address instead.

## Host and loopback

FastMCP runs the guard in `"auto"` mode, which treats the server differently
depending on where it is bound.

| Server bound to | `Host` header | An `Origin` that is a loopback address |
| --- | --- | --- |
| Loopback (`127.0.0.1`, the default) | Checked: a foreign host gets `421` | Admitted even if not listed |
| Anything else (`0.0.0.0` in a container) | Not checked | Judged by the list like any other |

On loopback this is what stops DNS rebinding: the attacker's page reaches the
server under the attacker's name, so its `Host` is wrong and the request is
refused, with or without an `Origin`. The cost is the second column: a page
served from another port on the same machine counts as local and is let in.

On `0.0.0.0` nothing checks `Host`, on purpose. A container is reached by its
service name, pod IP or a proxy hostname, and validating `Host` there would
turn every such request into a `421`. A request with no `Origin` is then not
judged at all. If that gap matters for a deployment, bind to loopback or put an
authenticating proxy in front.

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

## CORS is not included

Browsers have a second rule: a page may send a request to another origin, but
the page cannot read the reply unless the server grants it with CORS headers.
This feature does not add them. An origin on the list is let through by the
check, but a browser page on it still cannot read the replies. Anyone who needs
that adds CORS in front of the server, for example in a reverse proxy, using the
same origins.

## How it is built

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
origin_guard_kwargs(...)   (origin_guard.py)     -> run_app / create_app
      |
      v
FastMCP: host_origin_protection="auto", allowed_origins=[...]
```

1. **Parsing** (`parse_allowed_origins`). Splits the comma list, normalizes each
   entry (lowercase, default port dropped) using `urllib.parse.urlsplit` plus a
   few extra checks, and collects *all* problems into one `OriginListError`.
   An empty result means "nothing to enforce".

2. **Handover** (`origin_guard_kwargs`). Returns the two arguments FastMCP needs,
   or nothing when there is no list:

   ```python
   return {"host_origin_protection": "auto", "allowed_origins": list(origins)}
   ```

   `"auto"` is deliberate. `True` would validate `Host` on every request against
   loopback names only and answer `421` to any container or proxy. It must be
   passed together with the list, because FastMCP's default installs no guard
   at all and `allowed_origins` on its own would do nothing. With no list we
   pass nothing.

3. **Wiring.** `run_app` (`core/app.py`) splats the arguments into `mcp.run` for
   `http` and `sse`. With `--workers`, `create_app` in `src/mcp_server.py` splats
   them into `http_app` in each worker.

The value is also shown in the settings that the server reports
(`allowed_origins` in `get_server_configuration_status` and the startup
diagnostic).

## Where the tests are

- `tests/unit/test_origin_list_parsing.py`: the parser and its error messages.
- `tests/unit/test_origin_allowlist.py`: end-to-end behaviour through the real
  CLI, for single-process, `--workers` and SSE, in both the exposed and the
  loopback setup.

## Not done yet

- An audit event for rejected requests. FastMCP rejects before our code runs, so
  there is not even a log line for them; this needs a hook of our own.
- Rejecting requests that carry no `Origin`.
- An IP allow-list.
