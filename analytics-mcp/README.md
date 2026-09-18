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
| `create_index` | Create a secondary index on a collection via `CREATE INDEX` |
| `list_indexes` | List secondary indexes on collections, optionally filtered by database/scope/collection |
| `run_query_sync` | Run a SQL++ statement and buffer all result rows in memory; oversized results are truncated (see [Large results](#large-results)) |
| `explain_query` | Generate the query plan for a SQL++ statement using EXPLAIN (without executing it); pass the statement without an EXPLAIN keyword |

### Note on `create_index`

The `couchbase-analytics` SDK exposes **no index manager** — `Cluster` offers only
`database()`, `execute_query()`, `start_query()`, `set_credential()` and `shutdown()`,
and `Scope` only the two query methods. Unlike the parent `mcp-server-couchbase` server
(which uses the operational SDK's `collection.query_indexes().create_index(...)`),
`create_index` therefore has to build and execute a SQL++
[`CREATE INDEX`](https://docs.couchbase.com/enterprise-analytics/current/sqlpp/5_ddl_index.html)
statement.

Each entry in `fields` is one index element. A plain field is
`{"name": ..., "type": ...}`, where `type` is optional (`bigint`, `int`, `double`,
`string`, `date`, `time`, `datetime`); dotted paths address nested fields. To index
inside an array, use `unnest` instead of `name`, with `select` for arrays of objects:

```jsonc
// (`title`: string)
"fields": [{"name": "title", "type": "string"}]

// (`iata`)  -- type omitted
"fields": [{"name": "iata"}]

// (UNNEST `public_likes`: string)  -- array of primitives
"fields": [{"unnest": "public_likes", "type": "string"}]

// (`artist`, UNNEST `reviews` SELECT `ratings`.`Lyrics`: bigint)  -- mixed
"fields": [{"name": "artist"},
           {"unnest": "reviews", "select": [{"name": "ratings.Lyrics", "type": "bigint"}]}]
```

Optional clauses: `if_not_exists`, `exclude_unknown_key`, and `cast_default_null` /
`cast_formats` for `CAST (DEFAULT NULL ...)` — e.g.
`cast_formats={"date": "MM/DD/YYYY"}` emits `CAST (DEFAULT NULL DATE "MM/DD/YYYY")`,
used for TAV-backing indexes and non-ISO-8601 date formats.

Identifiers are backtick-quoted (embedded backticks doubled); the field *type* cannot be
quoted, so EA validates it. Invalid statements are forwarded to the server rather than
pre-validated, matching `run_sql_plus_plus_query` in the parent repo.

Behaviours verified against a live cluster that the published grammar does not state:

- A type is **optional** on plain fields, despite `IndexField ::= NestedField ":" IndexTypeRef`
  showing no optional marker. It is **mandatory** on array-indexed fields.
- Array indexes **must** pass `exclude_unknown_key=True` (`INCLUDE UNKNOWN KEY` is rejected too).
- `CAST` cannot be combined with an array index — *"CAST modifier is only allowed for B-Tree indexes"*.
- The type is not checked against the data: indexing a string field as `double` succeeds but
  silently indexes nothing. Use `get_schema_for_collection` when a field's type is unknown.

### Note on `list_indexes`

With no index manager in the SDK, `list_indexes` reads the
``System.Metadata.`Index` `` catalog directly. `database_name`, `scope_name` and
`collection_name` are all optional and bound as named parameters; passing none lists every
secondary index in the cluster.

Three classes of catalog row are excluded, because none is a user-created secondary index:

- **`System` database rows** — internal catalog indexes (`Dataverse`, `Dataset`, …).
- **Primary indexes** (`IsPrimary`) — in Analytics the primary index *is* the collection
  itself rather than a separate index, so the rows carry no extra information and
  `IsPrimary` is dropped from the result.
- **`IndexStructure = "SAMPLE"`** — collected samples the cost-based optimizer maintains,
  created by `ANALYZE COLLECTION`.

The catalog stores indexed fields in two different shapes, and both are returned exactly
as stored — read whichever is populated for a given index:

| Index kind | Populated field | Value |
| ---------- | --------------- | ----- |
| Scalar | `SearchKey` | `[["ratings", "Lyrics"]]` |
| Array of objects | `SearchKeyElements` | `[{"UnnestList": [["reviews"]], "ProjectList": [["ratings", "Lyrics"]]}]` |
| Array of primitives | `SearchKeyElements` | `[{"UnnestList": [["public_likes"]], "ProjectList": []}]` |

Each field path is an array of path components, so `["ratings", "Lyrics"]` is the nested
field `ratings.Lyrics`. Array indexes leave `SearchKey` **empty** and populate
`SearchKeyElements` instead — the read-back form of the `unnest`/`select` pair
`create_index` writes, where `UnnestList` is the arrays being unnested and `ProjectList`
the fields projected out of them (empty for an array of primitives). A listing that read
only `SearchKey` would therefore report array indexes as having no fields.

### Server Async Request API

Handle-based flow for long-running queries. Requires **EA 2.2+** and
`couchbase-analytics >= 1.1.0`.

| Tool Name | Description |
| --------- | ----------- |
| `run_query_async` | Submit a long-running query and return a `query_handle` token |
| `get_async_query_results` | Report whether the query has finished and, once it has, retrieve rows and metadata (repeatable); oversized results are truncated (see [Large results](#large-results)) |
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

## Large results

Query results above a size limit are truncated before being returned, so a big
result cannot flood the client's context. Optionally the full result is kept and
exposed as an MCP resource the client can read back.

| Option | Env var | Default | Purpose |
| ------ | ------- | ------- | ------- |
| `--save-large-results` | `EA_MCP_SAVE_LARGE_RESULTS` | off | Master switch for keeping oversized results |
| `--result-truncate-bytes` | `EA_MCP_RESULT_TRUNCATE_BYTES` | 1 MB | Size limit for inline results |
| `--result-storage-max-bytes` | `EA_MCP_RESULT_STORAGE_MAX_BYTES` | 1 GB | Total disk budget for saved results |
| `--result-storage-path` | `EA_MCP_RESULT_STORAGE_PATH` | temp dir | Where saved results are written |

`run_query_sync` and `get_async_query_results` each take
`save_result_if_large` (default `false`). Saving happens only when the server
switch **and** that per-call flag are both on **and** the result exceeds the
limit — a result that fits is always returned whole, never saved.

| Server switch | `save_result_if_large` | Result size | Behavior |
| ------------- | ---------------------- | ----------- | -------- |
| off | any | ≤ limit | returned whole |
| off | any | > limit | truncated only |
| on | `false` | > limit | truncated only |
| on | `true` | ≤ limit | returned whole, nothing saved |
| on | `true` | > limit | truncated **+** `result_id` and `resource_uri` |

Truncation is row-wise: rows are kept whole, so the client never receives a
partial JSON object. A truncated response reports `truncated: true` and
`total_row_count`, so the caller can tell how much was withheld.

### Reading a saved result

One resource template serves every read, returning JSON Lines (one row per
line):

```
ea://results/{result_id}                    # the whole result
ea://results/{result_id}?offset=100         # from row 100 to the end
ea://results/{result_id}?offset=0&limit=50  # a 50-row page
```

### Storage and eviction

Saved *sync* results are written to disk as `.jsonl`. When the total exceeds
the storage budget, the least recently used results are deleted — the files are
unlinked before the index entry is dropped, so the store never reports space it
has not actually reclaimed. A result larger than the entire budget is not saved
at all (evicting everything for something that still would not fit is worse than
declining), and the response says so.

Saved *async* results are **not** copied to disk. EA already holds the buffers,
so only the handle is recorded and the resource re-fetches from EA on each read.
Two consequences:

* The `result_id` **is** the `query_handle` — one id for the query and its
  resource, not two.
* The resource dies with the handle. Once `discard_async_query_results` or
  `cancel_async_query` runs, EA frees the rows and the URI stops working, so
  read it before discarding.

Like the handle registry, the result index is **per server process and
in-memory**: it does not survive a restart or reach another replica. Result
files orphaned by a crash are swept at startup.

## Tests

```bash
uv run pytest tests/unit -v
EA_CONNECTION_STRING=http://localhost:8095 EA_USERNAME=Administrator EA_PASSWORD=password \
  uv run pytest tests/integration -v
```
