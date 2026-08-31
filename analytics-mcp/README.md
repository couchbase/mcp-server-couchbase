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
| `create_index` | Create a secondary index on a collection via `CREATE INDEX` |
| `run_query_sync` | Run a SQL++ statement and buffer all result rows in memory |

### Note on `create_index`

The `couchbase-analytics` SDK exposes **no index manager** — `Cluster` offers only
`database()`, `execute_query()`, `start_query()`, `set_credential()` and `shutdown()`,
and `Scope` only the two query methods. Unlike the parent `mcp-server-couchbase` server
(which uses the operational SDK's `collection.query_indexes().create_index(...)`),
`create_index` therefore has to build and execute a SQL++
[`CREATE INDEX`](https://docs.couchbase.com/enterprise-analytics/current/sqlpp/5_ddl_index.html)
statement.

`fields` is a list of `{"name": ..., "type": ...}` objects, where `type` is **optional**
(supported types: `bigint`, `int`, `double`, `string`, `date`, `time`, `datetime`).
Dotted paths address nested fields:

```jsonc
// CREATE INDEX `song_title_idx` ON `music`.`myPlaylist`.`countrySongs` (`title`: string);
{"database_name": "music", "scope_name": "myPlaylist", "collection_name": "countrySongs",
 "index_name": "song_title_idx", "fields": [{"name": "title", "type": "string"}]}

// CREATE INDEX `name_idx` ON `travel-sample`.`inventory`.`airline` (`iata`);
{"database_name": "travel-sample", "scope_name": "inventory", "collection_name": "airline",
 "index_name": "name_idx", "fields": [{"name": "iata"}]}
```

> **On the optional type.** The published EBNF has
> `IndexField ::= NestedField ":" IndexTypeRef` with no optional marker, implying a type is
> always required. That appears to be a documentation bug: the same page's prose says
> *"Specify a type when using array indexes or a CAST modifier"*, its composite example
> indexes a bare `artist`, and a live cluster accepts
> `CREATE INDEX name_idx3 ON \`travel-sample\`.inventory.airline (iata)`. Types **are**
> mandatory for array indexes.

The tool covers standard scalar indexes (with optional `IF NOT EXISTS` and
`EXCLUDE UNKNOWN KEY`). Array (`UNNEST`) indexes are out of scope — use `run_query_sync`
for those.

## Tests

```bash
uv run pytest tests/unit -v
EA_CONNECTION_STRING=http://localhost:8095 EA_USERNAME=Administrator EA_PASSWORD=password \
  uv run pytest tests/integration -v
```
