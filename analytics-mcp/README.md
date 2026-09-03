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
| `run_query_sync` | Run a SQL++ statement and buffer all result rows in memory |
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

## Tests

```bash
uv run pytest tests/unit -v
EA_CONNECTION_STRING=http://localhost:8095 EA_USERNAME=Administrator EA_PASSWORD=password \
  uv run pytest tests/integration -v
```
