# Enterprise Analytics MCP Server — Tasks edition

A [FastMCP](https://github.com/jlowin/fastmcp) server that exposes Couchbase
**Enterprise Analytics** queries through the **MCP Tasks protocol** (2025-11-25
revision) instead of hand-built async tools.

## The design difference

The other two servers implement EA's *async request API* by hand — five tools
(`run_query_async`, `get_async_query_status`, `get_async_query_results`,
`discard_async_query_results`, `cancel_async_query`) plus a way to carry the
query handle between calls.

This server does the opposite:

- It uses EA's plain **blocking** `cluster.execute_query()`, which runs a query
  to completion in a single call.
- It registers that as a single tool with **`task=True`**, so the **MCP
  protocol** provides the "call-now, fetch-later" lifecycle:
  1. the client submits `run_query` as a task → gets a `taskId` immediately,
  2. FastMCP runs it in the background,
  3. the client polls `tasks/get` and retrieves rows via `tasks/result`.

So the async-ness lives at the **protocol layer**, not in our code. The tool
body just runs the query and returns rows.

| Server | Async lifecycle provided by | Query API | Multi-replica |
|--------|-----------------------------|-----------|---------------|
| `mcp-server-ea` | your 5 tools + in-memory registry | async handle | ❌ |
| `mcp-server-ea-rest` | your 5 tools + EA strings | async handle (REST) | ✅ |
| `mcp-server-ea-tasks` | **MCP Tasks protocol** | **blocking `execute_query()`** | config flip (see below) |

## Task backend

Task state/results are stored by `docket` (installed via `fastmcp[tasks]`):

- **`memory://`** (default) — in-process, no Redis. **Single process only.**
- **`redis://host:port/db`** — distributed; set `FASTMCP_DOCKET_URL`. Needed for
  multiple replicas or extra workers.

With the default `memory://` backend this server is single-process, like the
SDK+registry server. Making it multi-replica is a **configuration change**
(`FASTMCP_DOCKET_URL=redis://...`), not a code rewrite — because MCP Tasks was
designed around a durable external store.

## Requirements

- Python ≥ 3.10
- Enterprise Analytics 2.2+, `couchbase-analytics >= 1.1.0`
- `fastmcp[tasks]` (pulls in `docket`)
- A Tasks-capable MCP client (the client must support the Tasks protocol to
  submit/poll — a plain tool call will still work but runs synchronously).

## Configuration

| Flag | Env var | Default |
|------|---------|---------|
| `--endpoint` | `EA_ENDPOINT` | `http://localhost:9095` |
| `--username` | `EA_USERNAME` | — |
| `--password` | `EA_PASSWORD` | — |
| (task backend) | `FASTMCP_DOCKET_URL` | `memory://` |
| `--transport` | `EA_MCP_TRANSPORT` | `stdio` |

## Run

```bash
pip install -e .

EA_ENDPOINT=http://localhost:9095 \
EA_USERNAME=Administrator \
EA_PASSWORD=password \
enterprise-analytics-mcp-server-tasks --transport stdio
```
