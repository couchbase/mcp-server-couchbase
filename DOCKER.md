# Couchbase MCP Server

Pre-built images for the [Couchbase](https://www.couchbase.com/) MCP Server.

Couchbase MCP Server is a self-hosted MCP Server that allows AI agents to connect to and interact with data in Couchbase clusters, whether hosted on Capella or self-managed. It provides tools across categories including Cluster Health, Data Schema, Key-Value, Query, and Performance — with safety controls via read-only mode and fine-grained tool disabling. It supports both STDIO and Streamable HTTP transports.

Enterprise support for Couchbase MCP Server is available by licensing [Couchbase AI Data Plane](https://www.couchbase.com/downloads/?family=ai-data-plane), which also entitles use and enterprise support of Couchbase Agent Memory and Couchbase Agent Catalog.

GitHub Repo: <https://github.com/couchbase/mcp-server-couchbase>

Dockerfile: <https://github.com/couchbase/mcp-server-couchbase/blob/main/Dockerfile>

Documentation: <https://docs.couchbase.com/mcp-server/get-started/overview.html>

## Features/Tools

### Cluster setup & health tools

| Tool Name | Description |
| --------- | ----------- |
| `get_server_configuration_status` | Get the server status and configuration without connecting to the cluster — reports read-only mode, disabled/confirmation-required tools, OAuth settings, and the resolved logging configuration |
| `test_cluster_connection` | Check the cluster credentials by connecting to the cluster |
| `get_cluster_health_and_services` | Get cluster health status and list of all running services, optionally filtered to specific services via `service_types` |
| `get_cluster_diagnostics_report` | Get the SDK's cached connection diagnostics — whether connections were already broken and for how long, without any active network probing |
| `get_cluster_metrics` | Get one or more cluster statistics over a historic time window via the Management REST API's stats-range endpoint. **Self-managed Couchbase Server 7.6+ only — not available on Capella.** |
| `get_cluster_tasks` | Get the cluster tasks running right now — rebalance, compaction, XDCR, index build — via the Management REST API's tasks endpoint. Returns the raw task array; fields vary by task type. Requires the Read-Only Admin (`ro_admin`) role. **Self-managed Couchbase Server 7.6+ only — not available on Capella.** |
| `get_cluster_health_snapshot` | Get a per-node health snapshot — service topology, membership, orchestrator and a cluster health rollup — merged from the Management REST API's `/pools/default`, `nodeServices` and `terseClusterInfo` endpoints. Isolates a symptom to a specific node/service and flags which nodes are safe to act on. Requires the Read-Only Admin (`ro_admin`) role. **Self-managed Couchbase Server 7.6+ only — not available on Capella.** |
| `get_cluster_system_events` | Get the cluster's system event log — configuration changes, failovers, rebalances and service restarts with timestamps — via the Management REST API's `/events` endpoint. Correlates a symptom with what changed and when. Returns events oldest-first with a summary of counts and time range; windowed with `since_time` and bounded (default 50 events) rather than returning the endpoint's 250. Requires the Full Admin or Cluster Admin role. **Self-managed Couchbase Server 7.6+ only — not available on Capella.** |
| `discover_tool_input_values` | Look up the exact input values another tool needs, from reference data bundled with the server — currently every Couchbase Server metric name (type, unit, version added, description) for `get_cluster_metrics`. Browse by category or fuzzy-search by keyword. Works offline, without a cluster connection. |

### Data model & schema discovery tools

| Tool Name | Description |
| --------- | ----------- |
| `get_buckets_in_cluster` | Get a list of all the buckets in the cluster |
| `get_scopes_in_bucket` | Get a list of all the scopes in the specified bucket |
| `get_collections_in_scope` | Get a list of all the collections in a specified scope and bucket. Note that this tool requires the cluster to have Query service. |
| `get_scopes_and_collections_in_bucket` | Get a list of all the scopes and collections in the specified bucket |
| `get_schema_for_collection` | Get the structure for a collection |

### Document KV operations tools

| Tool Name | Description |
| --------- | ----------- |
| `get_document_by_id` | Get a document by ID from a specified scope and collection |
| `lookup_subdocument` | Look up parts of a document (specific fields, existence checks, or array/object counts) by path without fetching the whole document |
| `upsert_document_by_id` | Upsert a document by ID to a specified scope and collection. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `insert_document_by_id` | Insert a new document by ID (fails if document exists). **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `replace_document_by_id` | Replace an existing document by ID (fails if document doesn't exist). **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `delete_document_by_id` | Delete a document by ID from a specified scope and collection. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `mutate_subdocument` | Modify parts of an existing document (upsert, insert, replace, remove, array ops, counters) by path without rewriting the whole document. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |

### Query and indexing tools

| Tool Name | Description |
| --------- | ----------- |
| `list_indexes` | List all indexes in the cluster with their definitions, with optional filtering by bucket, scope, collection and index name. Set `return_raw_index_stats=true` to return the unprocessed index information. |
| `get_index_stats` | Get per-index statistics (size, fragmentation, scan traffic, indexing lag) from the Index Service, per node. Names which index is responsible for disk or memory pressure, and identifies unused indexes. **Self-managed Couchbase Server 7.6+ only — not available on Capella.** |
| `get_index_advisor_recommendations` | Get index recommendations from Couchbase Index Advisor for a given SQL++ query to optimize query performance |
| `create_index` | Create a scalar (non-vector) GSI secondary index on a collection. Deferred by default — call `build_index` afterward to build it. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `build_index` | Trigger the build of all deferred indexes on a collection. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `drop_index` | Drop a GSI index (scalar or vector) from a collection. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `run_sql_plus_plus_query` | Run a [SQL++ query](https://www.couchbase.com/sqlplusplus/) on a specified scope.<br><br>Queries are automatically scoped to the specified bucket and scope, so use collection names directly (e.g., `SELECT * FROM users` instead of `SELECT * FROM bucket.scope.users`).<br><br>`CB_MCP_READ_ONLY_MODE` is `true` by default, which means that **all write operations (KV, Query, index management, and FTS index management)** are disabled. When enabled (i.e. `CB_MCP_READ_ONLY_MODE=true`), write tools are not loaded and SQL++ queries that modify data are blocked. |
| `explain_sql_plus_plus_query` | Generate and evaluate an EXPLAIN plan for a SQL++ query. Returns query metadata, extracted plan, and plan evaluation findings. |

### Full-text search (FTS) tools

Requires Couchbase Server 7.6+ and the Search service. Vector search is not supported by these tools (see [Vector search tools](#vector-search-tools) below).

| Tool Name | Description |
| --------- | ----------- |
| `list_fts_indexes` | List Search (FTS) indexes. With no filters, lists cluster-level (legacy) indexes; with `bucket_name`, lists scope-level (scoped) indexes across every scope in that bucket; with `bucket_name` and `scope_name`, lists scope-level indexes in that one scope. |
| `get_fts_index_definition` | Get the full definition of a single Search index (mappings, analyzers, plan params). Pass `bucket_name` and `scope_name` together for a scope-level index, or omit both for a cluster-level (legacy) index. |
| `run_fts_query` | Run an FTS query against a Search index, or fetch its execution plan. `query` is the raw FTS query JSON body, supporting any non-vector query type (match, match_phrase, term, conjuncts, disjuncts, geo, date/numeric range, query_string, ...). Pass `explain=true` to fetch the execution plan instead of results — this still executes the query (`limit` defaulting to 1) since the Search service only exposes the plan per matched hit, not as a separate dry-run call. |
| `upsert_fts_index` | Create or update a Search (FTS) index definition (mappings, analyzers, plan params). Works with both scope-level (scoped) and cluster-level (legacy) indexes. Pass `bucket_name` and `scope_name` together to target a scope-level index, or omit both for a cluster-level (legacy) index. Updating an existing index triggers a full rebuild — fetch the current definition with `get_fts_index_definition` first and pass its `uuid` back to avoid clobbering concurrent changes. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `drop_fts_index` | Drop a Search (FTS) index. Works with both scope-level (scoped) and cluster-level (legacy) indexes. Pass `bucket_name` and `scope_name` together for a scope-level index, or omit both for a cluster-level (legacy) index. This permanently removes the index and cannot be undone — confirm the exact name and location with `list_fts_indexes` first. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |

### Vector search tools

Both tools embed query text using the model configured via `EMBEDDING_*` environment variables (see the table below) — the caller passes plain text, never a raw vector. `run_vector_search` targets Couchbase Server 8.0+'s GSI vector indexes via SQL++; `run_search_vector_search` targets the Search service's vector search on Couchbase Server 7.6+. Available on both self-managed Couchbase Server and Capella.

| Tool Name | Description |
| --------- | ----------- |
| `run_vector_search` | Embed a query and run a vector similarity search against a GSI vector index (Couchbase Server 8.0+), via SQL++'s `APPROX_VECTOR_DISTANCE()`. GSI selects the index automatically from the vector field referenced in the query — there is no `index_name` parameter. |
| `run_search_vector_search` | Run the Search service's vector search (Couchbase Server 7.6+) against a *named* Search index. `scalar_query` makes this a hybrid search (full-text and vector similarity both contribute to ranking); `prefilter` narrows the vector search's candidate pool before it runs (the Search-service equivalent of `run_vector_search`'s `where` prefilter). Both take the same raw FTS query JSON body `run_fts_query` accepts, and both are optional and combinable. |

### Query performance analysis tools

| Tool Name | Description |
| --------- | ----------- |
| `get_longest_running_queries` | Get longest running queries by average service time |
| `get_most_frequent_queries` | Get most frequently executed queries |
| `get_queries_with_largest_response_sizes` | Get queries with the largest response sizes |
| `get_queries_with_large_result_count` | Get queries with the largest result counts |
| `get_queries_using_primary_index` | Get queries that use a primary index (potential performance concern) |
| `get_queries_not_using_covering_index` | Get queries that don't use a covering index |
| `get_queries_not_selective` | Get queries that are not selective (index scans return many more documents than final result) |

### Query service health tools

These reach the Query service's own N1QL Admin REST API, distinct from the SQL++ system-catalog-based tools above — they report the query engine's health directly, per node, and include the only remediation action in this group. **Self-managed Couchbase Server 7.6+ only — not available on Capella.**

| Tool Name | Description |
| --------- | ----------- |
| `get_cluster_query_vitals` | Get query-engine health (request rate, active/queued request counts, memory, GC, uptime) from every query-service node, via the Query service's `/admin/vitals` endpoint. Distinguishes "the workload is heavy" from "the query engine itself is stressed." Requires at minimum the Read-Only Admin (`ro_admin`) role. |
| `get_active_queries` | Get all queries executing right now, merged across every query-service node, via the `/admin/active_requests` endpoint — elapsed time, statement, client, state. Requires at minimum the Read-Only Admin (`ro_admin`) role. |
| `delete_active_query` | Cancel an in-flight query by its request ID via `DELETE /admin/active_requests/{request_id}`, trying every query node in turn. Requires the Full Admin or Cluster Admin role. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |

### Operational Insights tools

This image also runs a second server, for Operational Insights clusters —
append `operational-insights` to the container's command to select it
instead of the default `operational` server (see [Configuration](#configuration)
below).

Every tool name below is prefixed with `oi_` (`get_server_configuration_status`
excepted) so none can collide with the operational server's tool names, even
if a single MCP client registers both servers at once.

| Tool Name | Description |
| --------- | ----------- |
| `get_server_configuration_status` | Get this server's status and configuration without connecting to a cluster — read-only mode, disabled/confirmation-required tools, OAuth settings, and the resolved logging configuration. Shared with the operational server: the same tool, registered by both. |
| `oi_get_databases_in_cluster` | List all databases in the Operational Insights cluster. |
| `oi_get_scopes_in_database` | List all scopes in a database. |
| `oi_get_collections_in_scope` | List all collections (datasets) in a scope. |
| `oi_get_schema_for_collection` | Infer the JSON schema of a collection by sampling documents. |
| `oi_list_indexes` | List secondary indexes via the `System.Metadata.Index` catalog. |
| `oi_run_query_sync` | Run a SQL++ statement (SELECT, DML, or DDL) and return all result rows. Enforces read-only mode server-side; there is no client-side SQL++ parser. |
| `oi_explain_query` | Generate the query plan for a SQL++ statement via EXPLAIN, without executing it. |
| `oi_create_index` | Create a secondary index via `CREATE INDEX`. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `oi_run_query_async` | Start a SQL++ statement without waiting for it to finish, returning a `query_handle` token. Same read-only enforcement as `oi_run_query_sync`. |
| `oi_get_async_query_results` | Check whether an async query has finished and, if so, return its rows. |
| `oi_discard_async_query_results` | Free a finished async query's result buffers on the server. |
| `oi_cancel_async_query` | Stop an async query that is still running. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |

The Server Async Request API tools form a start → poll → discard-or-cancel
flow: `oi_run_query_async` returns a `query_handle`, `oi_get_async_query_results` is
polled until ready, then `oi_discard_async_query_results` frees the results or
`oi_cancel_async_query` stops a still-running query.

## Usage

The Docker images can be used in the supported MCP clients such as Claude Desktop, Cursor, Windsurf, etc in combination with Docker.

### Configuration

Add the configuration specified below to the MCP configuration in your MCP client.

- Claude Desktop: <https://modelcontextprotocol.io/quickstart/user>
- Cursor: <https://docs.cursor.com/context/model-context-protocol#configuring-mcp-servers>
- Windsurf: <https://docs.windsurf.com/windsurf/cascade/mcp#adding-a-new-mcp-plugin>
- VS Code: <https://code.visualstudio.com/docs/copilot/customization/mcp-servers>
- JetBrains IDEs: <https://www.jetbrains.com/help/ai-assistant/model-context-protocol.html>

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-e",
        "CB_CONNECTION_STRING=<couchbase_connection_string>",
        "-e",
        "CB_USERNAME=<database_username>",
        "-e",
        "CB_PASSWORD=<database_password>",
        "docker.io/couchbase/mcp-server:latest"
      ]
    }
  }
}
```

To run the [Operational Insights server](#operational-insights-tools)
instead, append `operational-insights` to `args` and use its own env vars
(`CB_OI_CONNECTION_STRING`/`CB_OI_USERNAME`/`CB_OI_PASSWORD`, see below) —
with no trailing argument the container runs the default operational server:

```bash
docker run --rm -i \
  -e CB_OI_CONNECTION_STRING=http://localhost:8095 \
  -e CB_OI_USERNAME=Administrator \
  -e CB_OI_PASSWORD=password \
  docker.io/couchbase/mcp-server:latest operational-insights
```

### Environment Variables

The detailed explanation for the environment variables can be found on the [GitHub Repo](https://github.com/couchbase/mcp-server-couchbase?tab=readme-ov-file#additional-configuration-for-mcp-server).

| Variable                             | Description                                                                                                                                              | Default                                                        |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `CB_CONNECTION_STRING`               | Couchbase Connection string                                                                                                                              | **Required**                                                   |
| `CB_USERNAME`                        | Database username                                                                                                                                        | **Required (or Client Certificate and Key needed for mTLS)**   |
| `CB_PASSWORD`                        | Database password                                                                                                                                        | **Required (or Client Certificate and Key needed for mTLS)**   |
| `CB_CLIENT_CERT_PATH`                | Path to the client certificate file for mTLS authentication                                                                                              | **Required if using mTLS (or Username and Password required)** |
| `CB_CLIENT_KEY_PATH`                 | Path to the client key file for mTLS authentication                                                                                                      | **Required if using mTLS (or Username and Password required)** |
| `CB_CA_CERT_PATH`                    | Path to server root certificate for TLS if server is configured with a self-signed/untrusted certificate.                                                |                                                                |
| `CB_OI_CONNECTION_STRING`            | [Operational Insights server](#operational-insights-tools) endpoint URL (HTTP/HTTPS, not `couchbase://`). Ignored by the default `operational` server.  | **Required for `operational-insights`**                       |
| `CB_OI_USERNAME`                     | Operational Insights username. Ignored by the default `operational` server.                                                                              | **Required for `operational-insights`**                       |
| `CB_OI_PASSWORD`                     | Operational Insights password. Ignored by the default `operational` server.                                                                              | **Required for `operational-insights`**                       |
| `CB_OI_CA_CERT_PATH`                 | Path to server root certificate (PEM) for the Operational Insights server, if self-signed/untrusted. Ignored by the default `operational` server.        |                                                                |
| `CB_OI_CLIENT_CERT_PATH`             | Path to the client certificate for Operational Insights mTLS authentication (PEM, or a PKCS#12 bundle with `CB_OI_CLIENT_KEY_PATH` unset). Requires an `https://` `CB_OI_CONNECTION_STRING`; overrides username/password when set. Ignored by the default `operational` server. | **Required if using mTLS (or Username and Password required)** |
| `CB_OI_CLIENT_KEY_PATH`              | Path to the client certificate's private key (PEM) for Operational Insights mTLS. Leave unset for a PKCS#12 bundle. Ignored by the default `operational` server. | **Required if using mTLS (or Username and Password required)** |
| `CB_OI_CLIENT_CERT_PASSWORD`         | Decryption password for an encrypted Operational Insights client key/PKCS#12 bundle. Ignored by the default `operational` server.                        |                                                                |
| `CB_MCP_READ_ONLY_MODE`              | Prevent all data modifications (KV, Query, index management, and FTS index management). When `true`, write tools are not loaded.                                                               | `true`                                                         |
| `CB_MCP_TRANSPORT`                   | Transport mode (stdio/http/sse)                                                                                                                          | `stdio`                                                        |
| `CB_MCP_HOST`                        | Server host (HTTP/SSE modes)                                                                                                                             | `127.0.0.1`                                                    |
| `CB_MCP_PORT`                        | Server port (HTTP/SSE modes). Defaults to each server's own port when unset (`operational`: `8000`, `operational-insights`: `8001`) — set explicitly only to override. | `8000` (`operational`) / `8001` (`operational-insights`) |
| `CB_MCP_DISABLED_TOOLS`              | Tools to disable (see [Disabling Tools](#disabling-tools))                                                                                               | None                                                           |
| `CB_MCP_CONFIRMATION_REQUIRED_TOOLS` | Tools that require explicit user confirmation before execution (see [Elicitation/Confirmation for Tool Calls](#elicitationconfirmation-for-tool-calls))  | None                                                           |
| `CB_MCP_LOG_LEVEL`                   | Logging level for the server: `off`, `debug`, `info`, `warning`, `error` (see [Logging](#logging))                                                        | `info`                                                         |
| `CB_MCP_LOG_SINKS`                   | Comma-separated log destinations: `stderr`, `file`, or both (see [Logging](#logging))                                                                     | `stderr`                                                       |
| `CB_MCP_LOG_FILE`                    | Base path for per-level log files (only used when the `file` sink is enabled)                                                                             | `mcp_server.log`                                               |
| `CB_MCP_LOG_ROTATION_MAX_SIZE_MB`       | Global max size **in MB** per log file before it rotates, inherited by every level. `0` is invalid and falls back to the default with a startup warning    | `1` (1 MB)                                                     |
| `CB_MCP_LOG_MAX_BYTES`               | **Deprecated** — use `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` (MB). Global rotation size **in bytes**, still honored for backward compatibility; ignored when `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` is set | Unset                                     |
| `CB_MCP_LOG_ERROR_ROTATION_MAX_SIZE_MB`   | Rotation size **in MB** for the ERROR log file; overrides `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` for ERROR                                                        | Inherits `CB_MCP_LOG_ROTATION_MAX_SIZE_MB`                        |
| `CB_MCP_LOG_WARNING_ROTATION_MAX_SIZE_MB` | Rotation size **in MB** for the WARNING log file; overrides `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` for WARNING                                                    | Inherits `CB_MCP_LOG_ROTATION_MAX_SIZE_MB`                        |
| `CB_MCP_LOG_INFO_ROTATION_MAX_SIZE_MB`    | Rotation size **in MB** for the INFO log file; overrides `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` for INFO                                                          | Inherits `CB_MCP_LOG_ROTATION_MAX_SIZE_MB`                        |
| `CB_MCP_LOG_DEBUG_ROTATION_MAX_SIZE_MB`   | Rotation size **in MB** for the DEBUG log file; overrides `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` for DEBUG                                                        | Inherits `CB_MCP_LOG_ROTATION_MAX_SIZE_MB`                        |
| `CB_MCP_LOG_RETENTION_BACKUP_COUNT`  | Rotated backups kept per-level log file (excluding the live file), applied to every level unless overridden. `0` keeps only the live file            | `1`                                                            |
| `CB_MCP_LOG_ERROR_RETENTION_BACKUP_COUNT`   | Rotated backups kept for the ERROR log file; overrides the global count for ERROR                                                                  | Inherits `CB_MCP_LOG_RETENTION_BACKUP_COUNT`                   |
| `CB_MCP_LOG_WARNING_RETENTION_BACKUP_COUNT` | Rotated backups kept for the WARNING log file; overrides the global count for WARNING                                                              | Inherits `CB_MCP_LOG_RETENTION_BACKUP_COUNT`                   |
| `CB_MCP_LOG_INFO_RETENTION_BACKUP_COUNT`    | Rotated backups kept for the INFO log file; overrides the global count for INFO                                                                    | Inherits `CB_MCP_LOG_RETENTION_BACKUP_COUNT`                   |
| `CB_MCP_LOG_DEBUG_RETENTION_BACKUP_COUNT`   | Rotated backups kept for the DEBUG log file; overrides the global count for DEBUG                                                                  | Inherits `CB_MCP_LOG_RETENTION_BACKUP_COUNT`                   |
| `CB_MCP_OAUTH_JWT_JWKS_URI`          | JWKS endpoint of the identity provider used to verify bearer JWTs. Enables OAuth when set with the issuer and audience (see [OAuth 2.1 Authorization](#oauth-21-authorization)) | None                                            |
| `CB_MCP_OAUTH_JWT_ISSUER`            | Expected JWT `iss` claim. Required to enable OAuth                                                                                                        | None                                                           |
| `CB_MCP_OAUTH_JWT_AUDIENCE`          | Expected JWT `aud` claim. Required to enable OAuth                                                                                                        | None                                                           |
| `CB_MCP_OAUTH_JWT_ALGORITHM`         | JWT signing algorithm: one of `RS256/384/512`, `ES256/384/512`, `PS256/384/512`                                                                           | `RS256`                                                        |
| `CB_MCP_OAUTH_MCP_BASE_URL`          | Public base URL of this server. When set, publishes RFC 9728 Protected Resource Metadata for PRM-aware clients                                            | None                                                           |
| `CB_MCP_OAUTH_SCOPE_READ_LABEL`      | Override the OAuth scope label treated as 'read' access (advertised in PRM and matched against the token `scope`/`scp` claim). Use when your IdP can't emit the canonical form | `couchbase-mcp:read`                       |
| `CB_MCP_OAUTH_SCOPE_WRITE_LABEL`     | Override the OAuth scope label treated as 'write' access; same semantics as the read label                                                                | `couchbase-mcp:write`                                          |
| `EMBEDDING_PROVIDER`                 | Embedding provider for `run_vector_search` / `run_search_vector_search`: one of `couchbase`, `openai`, `cohere`, `voyage`, `bedrock`. Unset disables both tools' embedding step until configured. | None                                                           |
| `EMBEDDING_MODEL`                    | Model name/ID for the configured embedding provider                                                                                                       | None                                                           |
| `EMBEDDING_API_KEY`                  | API key for the configured provider. Not used by `bedrock` (uses the AWS credential chain / `EMBEDDING_AWS_*` instead)                                    | None                                                           |
| `EMBEDDING_ENDPOINT`                 | Base URL override (an OpenAI-compatible local server, or a Couchbase Model Service deployment's own URL — **required** when `EMBEDDING_PROVIDER=couchbase`) | None                                                         |
| `EMBEDDING_AWS_ACCESS_KEY_ID`        | AWS access key ID, `bedrock` provider only. Omit to use the default AWS credential chain                                                                  | None                                                           |
| `EMBEDDING_AWS_SECRET_ACCESS_KEY`    | AWS secret access key, `bedrock` provider only                                                                                                             | None                                                           |
| `EMBEDDING_AWS_REGION`               | AWS region, `bedrock` provider only. Falls back to the AWS SDK's own region resolution if unset                                                           | None                                                           |

### Disabling Tools

You can disable specific tools to prevent them from being loaded and exposed to the MCP client. Disabled tools will not appear in the tool discovery and cannot be invoked by the LLM.

#### Supported Formats

**Comma-separated list:**

```bash
# Environment variable
CB_MCP_DISABLED_TOOLS="upsert_document_by_id, delete_document_by_id"

# Command line
uvx couchbase-mcp-server --disabled-tools upsert_document_by_id, delete_document_by_id
```

**File path (one tool name per line):**

```bash
# Environment variable
CB_MCP_DISABLED_TOOLS=disabled_tools.txt

# Command line
uvx couchbase-mcp-server --disabled-tools disabled_tools.txt
```

**File format (e.g., `disabled_tools.txt`):**

```text
# Write operations
upsert_document_by_id
delete_document_by_id

# Index advisor
get_index_advisor_recommendations
```

Lines starting with `#` are treated as comments and ignored.

#### MCP Client Configuration Examples

**Using comma-separated list:**

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-e",
        "CB_CONNECTION_STRING=couchbases://connection-string",
        "-e",
        "CB_USERNAME=username",
        "-e",
        "CB_PASSWORD=password",
        "-e",
        "CB_MCP_DISABLED_TOOLS=upsert_document_by_id,delete_document_by_id",
        "docker.io/couchbase/mcp-server:latest"
      ]
    }
  }
}
```

**Using file path (recommended for many tools):**

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-v",
        "/path/to/disabled_tools.txt:/app/disabled_tools.txt",
        "-e",
        "CB_CONNECTION_STRING=couchbases://connection-string",
        "-e",
        "CB_USERNAME=username",
        "-e",
        "CB_PASSWORD=password",
        "-e",
        "CB_MCP_DISABLED_TOOLS=/app/disabled_tools.txt",
        "docker.io/couchbase/mcp-server:latest"
      ]
    }
  }
}
```

#### Important Security Note

> **Warning:** Disabling tools alone does not guarantee that certain operations cannot be performed. The underlying database user's RBAC (Role-Based Access Control) permissions are the authoritative security control.
>
> For example, even if you disable `upsert_document_by_id` and `delete_document_by_id`, data modifications can still occur via the `run_sql_plus_plus_query` tool using SQL++ DML statements (INSERT, UPDATE, DELETE, MERGE) unless:
>
> - The `CB_MCP_READ_ONLY_MODE` is set to `true` (default), which disables all write operations (KV, Query, index management, and FTS index management), OR
> - The database user lacks the necessary RBAC permissions for data modification
>
> **Best Practice:** Always configure appropriate RBAC permissions on your Couchbase user credentials as the primary security measure. Use `CB_MCP_READ_ONLY_MODE=true` (the default) for comprehensive write protection, and tool disabling as an additional layer to guide LLM behavior.

### Elicitation/Confirmation for Tool Calls

You can require explicit user confirmation for specific tools before execution (when the MCP client supports [elicitation](https://modelcontextprotocol.io/specification/2025-06-18/server/elicitation)).

#### Configuration Formats

**Comma-separated list:**

```bash
CB_MCP_CONFIRMATION_REQUIRED_TOOLS="delete_document_by_id,replace_document_by_id"
```

**File path (one tool name per line):**

```bash
CB_MCP_CONFIRMATION_REQUIRED_TOOLS=confirmation_tools.txt
```

**File format (e.g., `confirmation_tools.txt`):**

```text
# Destructive operations
delete_document_by_id
replace_document_by_id
```

Lines starting with `#` are treated as comments and ignored.

#### Behavior

When a listed tool is invoked:

- If the client supports elicitation, the user is prompted to confirm before execution.
- If the client does not support elicitation, the tool executes without confirmation for backward compatibility.

#### MCP Client Configuration Example

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-e",
        "CB_CONNECTION_STRING=couchbases://connection-string",
        "-e",
        "CB_USERNAME=username",
        "-e",
        "CB_PASSWORD=password",
        "-e",
        "CB_MCP_CONFIRMATION_REQUIRED_TOOLS=delete_document_by_id,replace_document_by_id",
        "docker.io/couchbase/mcp-server:latest"
      ]
    }
  }
}
```

### Logging

The server logs to `stderr` by default. Logging is configured with the `CB_MCP_LOG_*` variables in the [Environment Variables](#environment-variables) table:

- **`CB_MCP_LOG_LEVEL`** — how much is logged: `info` (the default) logs lifecycle events and tool invocations, `debug` adds verbose internal detail, and `off` disables all logging.
- **`CB_MCP_LOG_SINKS`** — where logs go: `stderr` (the default), per-level rotating files (`file`), or both. With `file`, one file is written per level (for example `mcp_server.info.log` and `mcp_server.error.log`) at the path set by `CB_MCP_LOG_FILE`. Mount a volume at that path to keep the logs after the container stops.
- **Rotation & retention** — rotation size is configured **in MB** via `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` (global) and per-level `CB_MCP_LOG_<LEVEL>_ROTATION_MAX_SIZE_MB` (inheriting the global); retention via `CB_MCP_LOG_RETENTION_BACKUP_COUNT` (global) and per-level `CB_MCP_LOG_<LEVEL>_RETENTION_BACKUP_COUNT`. A rotation size of `0` is invalid and falls back to the default with a startup warning. `CB_MCP_LOG_MAX_BYTES` (bytes) is deprecated but still honored for backward compatibility.
- **Server-config snapshot** — with the `file` sink active, a one-shot record is written as JSON to a dedicated `mcp_server_config.log.json` file (derived from `CB_MCP_LOG_FILE`), overwritten each start, so support always has the current config even after other logs rotate.

For more details, see the [documentation](https://docs.couchbase.com/mcp-server/configuration/logging.html).

### OAuth 2.1 Authorization

When running with `CB_MCP_TRANSPORT=http`, the server can act as an **OAuth 2.1 resource server**: it validates incoming bearer JWTs against your identity provider's JWKS. It is provider-agnostic (any OAuth 2.1 / OIDC provider that publishes a JWKS — Auth0, Okta, Keycloak, AWS Cognito, Microsoft Entra, etc.) and does **not** issue tokens or manage users. OAuth settings are ignored on `stdio`.

OAuth is configured with the `CB_MCP_OAUTH_*` variables in the [Environment Variables](#environment-variables) table:

- OAuth activates only when all three of `CB_MCP_OAUTH_JWT_JWKS_URI`, `CB_MCP_OAUTH_JWT_ISSUER`, and `CB_MCP_OAUTH_JWT_AUDIENCE` are set; setting only some of them fails at startup.
- Setting `CB_MCP_OAUTH_MCP_BASE_URL` additionally publishes RFC 9728 Protected Resource Metadata so PRM-aware clients can discover the authorization server.
- Access is gated by two scopes read from the token's `scope`/`scp` claim: `couchbase-mcp:read` (read tools, including SQL++) and `couchbase-mcp:write` (write tools: KV mutations, index management, and FTS index management). Full access requires both. If your IdP can't emit those canonical labels, override them with `CB_MCP_OAUTH_SCOPE_READ_LABEL` / `CB_MCP_OAUTH_SCOPE_WRITE_LABEL`.

For full details, see the [documentation](https://docs.couchbase.com/mcp-server/configuration/oauth-overview.html).
