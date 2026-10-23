# Couchbase MCP Server

Official images for the [Couchbase](https://www.couchbase.com/) MCP Server.

Couchbase MCP Server is a self-hosted [Model Context Protocol](https://modelcontextprotocol.io/) server that lets AI agents and assistants — Claude, Cursor, Windsurf, VS Code, JetBrains and other MCP clients — work with data in Couchbase Server clusters, on Capella or self-managed, and in self-managed Operational Insights clusters. Agents can explore schemas, run SQL++, full-text and vector queries, read and write documents, and diagnose query and cluster performance. It is read-only by default and supports STDIO and Streamable HTTP with OAuth 2.1.

Enterprise support is available by licensing [Couchbase AI Data Plane](https://www.couchbase.com/downloads/?family=ai-data-plane), which also entitles use and enterprise support of Couchbase Agent Memory and Couchbase Agent Catalog.

- **GitHub:** <https://github.com/couchbase/mcp-server-couchbase> — full configuration reference and tool list
- **Documentation:** <https://docs.couchbase.com/mcp-server/get-started/overview.html>
- **Dockerfile:** <https://github.com/couchbase/mcp-server-couchbase/blob/main/Dockerfile>

## Quick Start

### With an MCP client (stdio)

Add this to your MCP client's configuration ([Claude Desktop](https://modelcontextprotocol.io/quickstart/user), [Cursor](https://docs.cursor.com/context/model-context-protocol#configuring-mcp-servers), [Windsurf](https://docs.windsurf.com/windsurf/cascade/mcp#adding-a-new-mcp-plugin), [VS Code](https://code.visualstudio.com/docs/copilot/customization/mcp-servers) — which uses `servers` instead of `mcpServers` — or [JetBrains](https://www.jetbrains.com/help/ai-assistant/model-context-protocol.html)):

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "CB_CONNECTION_STRING=couchbases://connection-string",
        "-e", "CB_USERNAME=username",
        "-e", "CB_PASSWORD=password",
        "couchbase/mcp-server:latest", "operational"
      ]
    }
  }
}
```

The `-i` flag is required: on stdio, the client talks to the server over the container's stdin/stdout.

### As a shared HTTP server

```bash
docker run -d --name couchbase-mcp \
  -e CB_CONNECTION_STRING='couchbases://connection-string' \
  -e CB_USERNAME='username' \
  -e CB_PASSWORD='password' \
  -e CB_MCP_TRANSPORT=http \
  -e CB_MCP_HOST=0.0.0.0 \
  -p 8000:8000 \
  couchbase/mcp-server:latest operational
```

`CB_MCP_HOST=0.0.0.0` is required: the server binds to `127.0.0.1` by default, which is unreachable from outside the container. Clients then connect to `http://localhost:8000/mcp`:

```json
{
  "mcpServers": {
    "couchbase-http": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Without OAuth, the HTTP endpoint is unauthenticated — see [OAuth](#oauth-21) before exposing it beyond localhost.

## Choosing a Server

The image ships two servers. Select one with the container command:

| Command | Connects to | Credential variables | Default port |
| --- | --- | --- | --- |
| `operational` (default) | A Couchbase cluster | `CB_CONNECTION_STRING`, `CB_USERNAME`, `CB_PASSWORD`, … | `8000` |
| `operational-insights` | A self-managed [Operational Insights](https://docs.couchbase.com/operational-insights/current/intro/intro.html) cluster — a real-time analytical database (RT-OLAP); not available on Capella | `CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING`, `CB_OPERATIONAL_INSIGHTS_USERNAME`, `CB_OPERATIONAL_INSIGHTS_PASSWORD`, … | `8001` |

With no command, the container runs `operational`; naming it explicitly is recommended.

```bash
docker run --rm -i \
  -e CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING=http://host.docker.internal:8095 \
  -e CB_OPERATIONAL_INSIGHTS_USERNAME=Administrator \
  -e CB_OPERATIONAL_INSIGHTS_PASSWORD=password \
  couchbase/mcp-server:latest operational-insights
```

The Operational Insights connection string is an **HTTP(S) URL**, not `couchbase://`.

## Tags

| Tag | Points to |
| --- | --- |
| `X.Y.Z` (e.g. `1.0.1`) | That exact release. Pin this in production |
| `X.Y` (e.g. `1.0`) | The latest stable patch release of that minor version |
| `latest` | The most recent stable release |
| `X.Y.ZrcN` (e.g. `1.0.2rc4`) | A pre-release, for testing. Pre-releases never move `X.Y` or `latest` |

## Image Details

- **User:** runs as non-root `mcpuser` (UID `1001`). Mounted directories the server writes to must be writable by UID 1001.
- **Working directory:** `/app`. Relative paths (such as the default log file) resolve here.
- **Entrypoint:** `couchbase-mcp-server` (exec form, so the server is PID 1). The container command selects the server and takes CLI flags, e.g. `operational --log-level=debug`.
- **Defaults:** `CB_MCP_TRANSPORT=stdio`, `CB_MCP_READ_ONLY_MODE=true`.
- **Exposed ports:** `8000` (`operational`) and `8001` (`operational-insights`), used only by the `http` and `sse` transports.
- **Base:** `python:3.13-slim-trixie`. There is no `curl` or `wget` in the image — use Python for health checks (see below).
- **Labels:** `org.opencontainers.image.revision` and `org.opencontainers.image.created` record the source commit and build time.

## Configuration

The most common variables are below. Every option — including logging rotation, auditing, OAuth, workers and embedding providers — is listed in the [full configuration reference](https://github.com/couchbase/mcp-server-couchbase#all-options).

| Variable | Description | Default |
| --- | --- | --- |
| `CB_CONNECTION_STRING` | Couchbase connection string | **Required** |
| `CB_USERNAME` / `CB_PASSWORD` | Basic authentication | Required, unless using mTLS |
| `CB_CLIENT_CERT_PATH` / `CB_CLIENT_KEY_PATH` | mTLS authentication (used instead of username/password when set) | |
| `CB_CA_CERT_PATH` | Root certificate for a self-signed/untrusted server certificate | |
| `CB_MCP_READ_ONLY_MODE` | When `true`, write tools are not loaded and modifying SQL++ is blocked | `true` |
| `CB_MCP_TRANSPORT` | `stdio`, `http`, or `sse` (deprecated) | `stdio` |
| `CB_MCP_HOST` | Bind address for HTTP — set `0.0.0.0` in containers | `127.0.0.1` |
| `CB_MCP_PORT` | Port for HTTP | `8000` / `8001` |
| `CB_MCP_WORKERS` | Worker processes for `http` (`operational` only) | `1` |
| `CB_MCP_DISABLED_TOOLS` | Tools to hide — comma-separated list or a file path | |
| `CB_MCP_CONFIRMATION_REQUIRED_TOOLS` | Tools that ask the user to confirm first — comma-separated list or a file path | |
| `CB_MCP_LOG_LEVEL` | `off`, `error`, `warning`, `info`, `debug` | `info` |
| `CB_MCP_LOG_SINKS` | `stderr`, `file`, or both | `stderr` |
| `CB_MCP_LOG_FILE` | Base path for log files when the `file` sink is on | `mcp_server.log` |
| `CB_MCP_AUDIT_LOG_ENABLED` | Turn on audit logging | `false` |
| `CB_MCP_AUDIT_LOG_SINKS` / `CB_MCP_AUDIT_LOG_FILE_PATH` | `console` and/or `file`, and the audit file path | `console` |
| `CB_MCP_OAUTH_JWT_JWKS_URI` / `_ISSUER` / `_AUDIENCE` | Turn on OAuth 2.1 (all three required) | |
| `CB_MCP_EMBEDDING_MODEL_PROVIDER` | Embedding provider for the vector search tools: `capella`, `openai`, `cohere`, `voyage`, `bedrock` | |

> [!IMPORTANT]
> Disabling tools does not prevent writes on its own — `run_sql_plus_plus_query` can still run DML unless read-only mode is on. The database user's RBAC permissions are the authoritative security control.

## Volumes and Files

Files the server reads (certificates, tool lists) and writes (logs, audit records) live inside the container unless you mount them. Mount a volume for anything you need to keep after the container is removed.

```bash
docker run --rm -i \
  -v /path/to/certs:/certs:ro \
  -v /path/to/disabled_tools.txt:/config/disabled_tools.txt:ro \
  -v /path/to/logs:/logs \
  -v /path/to/audit:/audit \
  -e CB_CONNECTION_STRING=couchbases://connection-string \
  -e CB_CLIENT_CERT_PATH=/certs/client.pem \
  -e CB_CLIENT_KEY_PATH=/certs/client.key \
  -e CB_MCP_DISABLED_TOOLS=/config/disabled_tools.txt \
  -e CB_MCP_LOG_SINKS=stderr,file \
  -e CB_MCP_LOG_FILE=/logs/mcp_server.log \
  -e CB_MCP_AUDIT_LOG_ENABLED=true \
  -e CB_MCP_AUDIT_LOG_SINKS=file \
  -e CB_MCP_AUDIT_LOG_FILE_PATH=/audit/audit.log \
  couchbase/mcp-server:latest operational
```

The `/logs` and `/audit` host directories must be writable by UID 1001.

**Audit files in containers.** The hostname and process id are inserted into the audit file name (`audit.log` → `audit.<hostname>.1.log`). Because the server is always PID 1, the container's hostname is what keeps files from different containers apart on a shared volume — Docker gives each container a distinct hostname by default. If you set the same `--hostname` on several containers that share one volume, give each its own subdirectory. Retention (`CB_MCP_AUDIT_LOG_RETENTION_MAX_BACKUPS`) applies per file, and nothing prunes the files of containers that have exited, so size the volume for the number of container starts or sweep old `audit.*.log*` files.

## Networking

- If Couchbase runs on the Docker host, use `couchbase://host.docker.internal` as the connection string (see [Docker networking](https://docs.docker.com/desktop/features/networking/#i-want-to-connect-from-a-container-to-a-service-on-the-host)).
- If Couchbase runs in another container, put both on the same network with `--network=<your_network>` and use the Couchbase container's name as the host.
- For Capella, make sure the cluster [allows connections](https://docs.couchbase.com/cloud/clusters/allow-ip-address.html) from the Docker host's IP address.

## Health Checks

With `CB_MCP_TRANSPORT=http`, the server answers `GET /health` with `{"status": "ok", "server": "<server>"}` on the MCP port. It is a liveness check: it does not connect to the cluster, needs no authentication even with OAuth on, and is not available on `stdio`.

Kubernetes:

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
```

Docker Compose (the image has no `curl`, so the check uses Python):

```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
  interval: 10s
  timeout: 5s
  retries: 3
```

Use port `8001` for `operational-insights`, or whatever `CB_MCP_PORT` is set to.

## OAuth 2.1

In HTTP mode the server can validate bearer JWTs from any OAuth 2.1/OIDC provider that publishes a JWKS (Auth0, Okta, Keycloak, Cognito, Entra, …). Set `CB_MCP_OAUTH_JWT_JWKS_URI`, `CB_MCP_OAUTH_JWT_ISSUER` and `CB_MCP_OAUTH_JWT_AUDIENCE` (all three are required), and optionally `CB_MCP_OAUTH_MCP_BASE_URL` to publish Protected Resource Metadata. Tokens need the `couchbase-mcp:read` scope for read tools and `couchbase-mcp:write` for write tools. See the [OAuth documentation](https://docs.couchbase.com/mcp-server/configuration/oauth-overview.html).

## Tools

Write tools are only loaded when `CB_MCP_READ_ONLY_MODE=false`. Some admin tools need self-managed Couchbase Server 7.6+ and are not registered on Capella. See the [full tool list](https://github.com/couchbase/mcp-server-couchbase#operational-tools) for descriptions.

**`operational` server**

- **Cluster setup and health:** `get_server_configuration_status`, `test_cluster_connection`, `get_cluster_health_and_services`, `get_client_connectivity_report`, `get_cluster_metrics`, `get_cluster_tasks`, `get_cluster_health_snapshot`, `get_bucket_config`, `get_cluster_system_events`, `discover_tool_input_values`
- **Data model and schema:** `get_buckets_in_cluster`, `get_scopes_in_bucket`, `get_collections_in_scope`, `get_scopes_and_collections_in_bucket`, `get_schema_for_collection`, `create_scope`, `create_collection`, `delete_scope`, `delete_collection`
- **Documents (KV):** `get_document_by_id`, `lookup_subdocument`, `upsert_document_by_id`, `insert_document_by_id`, `replace_document_by_id`, `delete_document_by_id`, `mutate_subdocument`
- **Query and indexing:** `run_sql_plus_plus_query`, `explain_sql_plus_plus_query`, `get_index_advisor_recommendations`, `list_indexes`, `get_index_stats`, `create_query_index`, `create_index` (deprecated), `build_index`, `drop_index`
- **Full-text search:** `list_search_indexes`, `get_search_index_definition`, `run_search`, `upsert_search_index`, `drop_search_index`
- **Vector search:** `run_vector_query`, `run_vector_search`
- **Query performance:** `get_longest_running_queries`, `get_most_frequent_queries`, `get_queries_with_largest_response_sizes`, `get_queries_with_large_result_count`, `get_queries_using_primary_index`, `get_queries_not_using_covering_index`, `get_queries_not_selective`
- **Query service health:** `get_cluster_query_vitals`, `get_active_queries`, `delete_active_query`

**`operational-insights` server**

`get_server_configuration_status`, `operational_insights_get_databases_in_cluster`, `operational_insights_get_scopes_in_database`, `operational_insights_get_collections_in_scope`, `operational_insights_get_schema_for_collection`, `operational_insights_list_indexes`, `operational_insights_explain_query`, `operational_insights_run_query_sync`, `operational_insights_run_query_async`, `operational_insights_get_async_query_results`, `operational_insights_discard_async_query_results`, `operational_insights_cancel_async_query`, `operational_insights_create_index`
