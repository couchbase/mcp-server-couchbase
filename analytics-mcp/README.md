# couchbase-analytics-mcp-server (prototype)

A small demo FastMCP server exposing tools for Couchbase Enterprise Analytics (EA),
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
| `explain_query` | Generate the query plan for a SQL++ statement using EXPLAIN (without executing it); pass the statement without an EXPLAIN keyword |

## Tests

```bash
uv run pytest tests/unit -v
EA_CONNECTION_STRING=http://localhost:8095 EA_USERNAME=Administrator EA_PASSWORD=password \
  uv run pytest tests/integration -v
```
