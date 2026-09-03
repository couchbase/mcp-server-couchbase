# couchbase-analytics-mcp-server (prototype)

A small, throwaway FastMCP server exposing tools for Couchbase Enterprise Analytics (EA),
built on the `couchbase-analytics` Python SDK (not the operational `couchbase` SDK used by
the parent `mcp-server-couchbase` server).

This is a prototype for validating the tool set and writing real unit/integration tests
against a live EA cluster — it deliberately has **no OAuth, no scope enforcement, no
read-only-mode toggle**. Its tool bodies and tests are written to be copy-pasted into
whatever the final EA architecture turns out to be.

## Running

```bash
uv sync --extra dev
EA_CONNECTION_STRING=http://localhost:8095 EA_USERNAME=Administrator EA_PASSWORD=password \
  uv run ea-mcp-server
```

## Tools

| Tool Name | Description |
| --------- | ----------- |
| `get_databases_in_cluster` | List all databases in the cluster |
| `get_scopes_in_database` | List all scopes in a database |
| `get_collections_in_scope` | List all collections (datasets) in a scope |
| `get_schema_for_collection` | Infer the JSON schema of a collection by sampling documents |
| `run_query_sync` | Run a SQL++ statement and buffer all result rows in memory |

### Server Async Request API

Handle-based flow for long-running queries. Requires **EA 2.2+** and
`couchbase-analytics >= 1.1.0`.

| Tool Name | Description |
| --------- | ----------- |
| `run_query_async` | Submit a long-running query and return a `query_handle` token |
| `get_async_query_results` | Report whether the query has finished and, once it has, retrieve rows and metadata (repeatable) |
| `discard_async_query_results` | Release server-side result buffers |
| `cancel_async_query` | Cancel the query associated with the handle |

Typical flow:

```
run_query_async -> query_handle
  -> get_async_query_results         (ready: false while running; rows once ready)
  -> discard_async_query_results     (free buffers; ends the lifecycle)
  or cancel_async_query              (stop a still-running query)
```

`get_async_query_results` doubles as the readiness check — it returns
`ready: false` while the query is still running — so there is no separate
status tool.

**Fetching does not free results.** EA keeps the result buffers after a fetch —
verified against EA 2.2, where the result URL still returns `200` post-fetch and
only `404`s after a discard. So `get_async_query_results` can be called more than
once, and the `query_handle` stays valid until `discard_async_query_results` or
`cancel_async_query` evicts it. A caller that never discards leaves buffers
allocated on the EA server until EA times them out.

The SDK's live `QueryHandle` objects cannot be serialized, so they are held in a
server-side registry and referenced by an opaque `query_handle` token (see
`src/ea_mcp/handle_registry.py`). The registry is **per server process and
in-memory**: a token is only valid within the session that created it, and does
not survive a restart or reach another replica.

## Tests

```bash
uv run pytest tests/unit -v
EA_CONNECTION_STRING=http://localhost:8095 EA_USERNAME=Administrator EA_PASSWORD=password \
  uv run pytest tests/integration -v
```
