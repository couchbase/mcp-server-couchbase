# Enterprise Analytics MCP Server — Stateless (REST)

A [FastMCP](https://github.com/jlowin/fastmcp) server exposing Couchbase
**Enterprise Analytics** (EA) Server Async Request API as MCP tools,
implemented **directly against EA's REST endpoints** — no `couchbase-analytics`
SDK, no server-side handle registry.

## Why REST / why stateless

The SDK-based server (`../mcp-server-ea`) keeps live `QueryHandle` objects in a
per-process registry. Those objects can't be serialized (they hold a thread
lock) or shared, so that design breaks across multiple replicas and across
restarts.

This server holds **no per-query state**. Query identity travels entirely in
*strings* — EA's `request_id` and result-handle URLs — which are returned to the
client and passed back on each call. Every tool call is a single self-contained
HTTP request to EA, which is the true owner of the query state. Result:

- ✅ works across multiple replicas (any replica can service any call)
- ✅ survives server restarts (nothing to lose)
- ✅ nothing to share between processes

The only server-side object is a shared `httpx` connection pool.

> **Note — bearer tokens:** the `request_id` / handle strings are bearer
> capabilities: anyone holding the string can act on that query. There is no
> per-client ownership check. Add identity scoping on top if clients are
> mutually untrusted.

## Tools

| Tool | REST call | Input | Output |
|------|-----------|-------|--------|
| `run_query_async` | `POST /api/v1/request` (`mode:async`) | `statement` | `request_id`, `status_handle` |
| `get_async_query_status` | `GET <status_handle>` | `status_handle` | `ready`, `result_handle?` |
| `get_async_query_results` | `GET <result_handle>` | `result_handle` | `rows`, `metrics` |
| `discard_async_query_results` | `DELETE <result_handle>` | `result_handle` | `discarded` |
| `cancel_async_query` | `DELETE /api/v1/active_requests?request_id=…` | `request_id` | `cancelled` |
| `list_async_queries` | `GET /api/v1/active_requests` | — | `queries[]` (each with `request_id`, `statement`, `state`) |

`list_async_queries` recovers a lost `request_id` straight from EA. Because EA
is the source of truth, it lists queries started by **any** replica and
survives this server's restarts. EA reports only *running* requests, so it
recovers cancellable in-flight queries — not the result handles of completed
ones.

## Requirements

- Python ≥ 3.10
- A running Enterprise Analytics cluster (2.2+)
- `httpx` (no analytics SDK required)

## Configuration

| Flag | Env var | Default |
|------|---------|---------|
| `--endpoint` | `EA_ENDPOINT` | `http://localhost:9095` (query port, not console `9091`) |
| `--username` | `EA_USERNAME` | — |
| `--password` | `EA_PASSWORD` | — |
| `--tls-verify/--no-tls-verify` | `EA_TLS_VERIFY` | verify on |
| `--transport` | `EA_MCP_TRANSPORT` | `stdio` |
| `--host` / `--port` | `EA_MCP_HOST` / `EA_MCP_PORT` | `127.0.0.1` / `8000` |

## Run

```bash
pip install -e .

EA_ENDPOINT=http://localhost:9095 \
EA_USERNAME=Administrator \
EA_PASSWORD=password \
enterprise-analytics-mcp-server-rest --transport stdio
```
