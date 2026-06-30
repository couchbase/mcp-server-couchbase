# Couchbase MCP Server

Pre-built images for the [Couchbase](https://www.couchbase.com/) MCP Server.

Couchbase MCP Server is a self-hosted MCP Server that allows AI agents to connect to and interact with data in Couchbase clusters, whether hosted on Capella or self-managed. It provides tools across categories including Cluster Health, Data Schema, Key-Value, Query, and Performance — with safety controls via read-only mode and fine-grained tool disabling. It supports both STDIO and Streamable HTTP transports.

Enterprise support for Couchbase MCP Server is available by licensing [Couchbase AI Data Plane](https://www.couchbase.com/downloads/?family=ai-data-plane), which also entitles use and enterprise support of Couchbase Agent Memory and Couchbase Agent Catalog.

GitHub Repo: <https://github.com/couchbase/mcp-server-couchbase>

Dockerfile: <https://github.com/couchbase/mcp-server-couchbase/blob/main/Dockerfile>

Documentation: <https://mcp-server.couchbase.com>

## Features/Tools

### Cluster setup & health tools

| Tool Name | Description |
| --------- | ----------- |
| `get_server_configuration_status` | Get the server status and configuration without connecting to the cluster — reports read-only mode, disabled/confirmation-required tools, OAuth settings, and the resolved logging configuration |
| `test_cluster_connection` | Check the cluster credentials by connecting to the cluster |
| `get_cluster_health_and_services` | Get cluster health status and list of all running services |

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
| `upsert_document_by_id` | Upsert a document by ID to a specified scope and collection. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `insert_document_by_id` | Insert a new document by ID (fails if document exists). **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `replace_document_by_id` | Replace an existing document by ID (fails if document doesn't exist). **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |
| `delete_document_by_id` | Delete a document by ID from a specified scope and collection. **Disabled by default when `CB_MCP_READ_ONLY_MODE=true`.** |

### Query and indexing tools

| Tool Name | Description |
| --------- | ----------- |
| `list_indexes` | List all indexes in the cluster with their definitions, with optional filtering by bucket, scope, collection and index name. Set `return_raw_index_stats=true` to return the unprocessed index information. |
| `get_index_advisor_recommendations` | Get index recommendations from Couchbase Index Advisor for a given SQL++ query to optimize query performance |
| `run_sql_plus_plus_query` | Run a [SQL++ query](https://www.couchbase.com/sqlplusplus/) on a specified scope.<br><br>Queries are automatically scoped to the specified bucket and scope, so use collection names directly (e.g., `SELECT * FROM users` instead of `SELECT * FROM bucket.scope.users`).<br><br>`CB_MCP_READ_ONLY_MODE` is `true` by default, which means that **all write operations (KV and Query)** are disabled. When enabled, KV write tools are not loaded and SQL++ queries that modify data are blocked. |
| `explain_sql_plus_plus_query` | Generate and evaluate an EXPLAIN plan for a SQL++ query. Returns query metadata, extracted plan, and plan evaluation findings. |

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
| `CB_MCP_READ_ONLY_MODE`              | Prevent all data modifications (KV and Query). When `true`, KV write tools are not loaded.                                                               | `true`                                                         |
| `CB_MCP_TRANSPORT`                   | Transport mode (stdio/http/sse)                                                                                                                          | `stdio`                                                        |
| `CB_MCP_HOST`                        | Server host (HTTP/SSE modes)                                                                                                                             | `127.0.0.1`                                                    |
| `CB_MCP_PORT`                        | Server port (HTTP/SSE modes)                                                                                                                             | `8000`                                                         |
| `CB_MCP_DISABLED_TOOLS`              | Tools to disable (see [Disabling Tools](#disabling-tools))                                                                                               | None                                                           |
| `CB_MCP_CONFIRMATION_REQUIRED_TOOLS` | Tools that require explicit user confirmation before execution (see [Elicitation/Confirmation for Tool Calls](#elicitationconfirmation-for-tool-calls))  | None                                                           |
| `CB_MCP_LOG_LEVEL`                   | Logging level for the server: `off`, `debug`, `info`, `warning`, `error` (see [Logging](#logging))                                                        | `info`                                                         |
| `CB_MCP_LOG_SINKS`                   | Comma-separated log destinations: `stderr`, `file`, or both (see [Logging](#logging))                                                                     | `stderr`                                                       |
| `CB_MCP_LOG_FILE`                    | Base path for per-level log files (only used when the `file` sink is enabled)                                                                             | `mcp_server.log`                                               |
| `CB_MCP_LOG_MAX_BYTES`               | Maximum size in bytes per log file before it rotates                                                                                                      | `1048576` (1 MB)                                               |
| `CB_MCP_OAUTH_JWT_JWKS_URI`          | JWKS endpoint of the identity provider used to verify bearer JWTs. Enables OAuth when set with the issuer and audience (see [OAuth 2.1 Authorization](#oauth-21-authorization)) | None                                            |
| `CB_MCP_OAUTH_JWT_ISSUER`            | Expected JWT `iss` claim. Required to enable OAuth                                                                                                        | None                                                           |
| `CB_MCP_OAUTH_JWT_AUDIENCE`          | Expected JWT `aud` claim. Required to enable OAuth                                                                                                        | None                                                           |
| `CB_MCP_OAUTH_JWT_ALGORITHM`         | JWT signing algorithm: one of `RS256/384/512`, `ES256/384/512`, `PS256/384/512`                                                                           | `RS256`                                                        |
| `CB_MCP_OAUTH_MCP_BASE_URL`          | Public base URL of this server. When set, publishes RFC 9728 Protected Resource Metadata for PRM-aware clients                                            | None                                                           |

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
> - The `CB_MCP_READ_ONLY_MODE` is set to `true` (default), which disables all write operations (KV and Query), OR
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

For more details, see the [documentation](https://mcp-server.couchbase.com/configuration/logging).

### OAuth 2.1 Authorization

When running with `CB_MCP_TRANSPORT=http`, the server can act as an **OAuth 2.1 resource server**: it validates incoming bearer JWTs against your identity provider's JWKS. It is provider-agnostic (any OAuth 2.1 / OIDC provider that publishes a JWKS — Auth0, Okta, Keycloak, AWS Cognito, Microsoft Entra, etc.) and does **not** issue tokens or manage users. OAuth settings are ignored on `stdio`.

OAuth is configured with the `CB_MCP_OAUTH_*` variables in the [Environment Variables](#environment-variables) table:

- OAuth activates only when all three of `CB_MCP_OAUTH_JWT_JWKS_URI`, `CB_MCP_OAUTH_JWT_ISSUER`, and `CB_MCP_OAUTH_JWT_AUDIENCE` are set; setting only some of them fails at startup.
- Setting `CB_MCP_OAUTH_MCP_BASE_URL` additionally publishes RFC 9728 Protected Resource Metadata so PRM-aware clients can discover the authorization server.
- Access is gated by two scopes read from the token's `scope`/`scp` claim: `couchbase-mcp:read` (read tools, including SQL++) and `couchbase-mcp:write` (KV mutation tools). Full access requires both.

For full details, see the [documentation](https://mcp-server.couchbase.com/configuration/oauth).
