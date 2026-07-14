# Couchbase MCP Server

Couchbase MCP Server is a self-hosted MCP Server that allows AI agents to connect to and interact with data in Couchbase clusters, whether hosted on Capella or self-managed. It provides tools across categories including Cluster Health, Data Schema, Key-Value, Query, and Performance — with safety controls via read-only mode and fine-grained tool disabling. It supports both STDIO and Streamable HTTP transports.

Couchbase MCP server is distributed as a Python Package Index (PyPI) package and via Docker.
Enterprise support for Couchbase MCP Server is available by licensing [Couchbase AI Data Plane](https://www.couchbase.com/downloads/?family=ai-data-plane), which also entitles use and enterprise support of Couchbase Agent Memory and Couchbase Agent Catalog.

[![Docs](https://img.shields.io/badge/Docs-1B9E5A?logo=docusaurus&logoColor=white)](https://mcp-server.couchbase.com/) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![PyPI version](https://badge.fury.io/py/couchbase-mcp-server.svg)](https://pypi.org/project/couchbase-mcp-server/) [![Install in Cursor](https://img.shields.io/badge/Cursor-Install_Server-1e1e1e?logo=data:image/svg%2bxml;base64,PHN2ZyBoZWlnaHQ9IjFlbSIgc3R5bGU9ImZsZXg6bm9uZTtsaW5lLWhlaWdodDoxIiB2aWV3Qm94PSIwIDAgMjQgMjQiIHdpZHRoPSIxZW0iCiAgICB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPgogICAgPHRpdGxlPkN1cnNvcjwvdGl0bGU+CiAgICA8cGF0aCBkPSJNMTEuOTI1IDI0bDEwLjQyNS02LTEwLjQyNS02TDEuNSAxOGwxMC40MjUgNnoiCiAgICAgICAgZmlsbD0idXJsKCNsb2JlLWljb25zLWN1cnNvcnVuZGVmaW5lZC1maWxsLTApIj48L3BhdGg+CiAgICA8cGF0aCBkPSJNMjIuMzUgMThWNkwxMS45MjUgMHYxMmwxMC40MjUgNnoiIGZpbGw9InVybCgjbG9iZS1pY29ucy1jdXJzb3J1bmRlZmluZWQtZmlsbC0xKSI+PC9wYXRoPgogICAgPHBhdGggZD0iTTExLjkyNSAwTDEuNSA2djEybDEwLjQyNS02VjB6IiBmaWxsPSJ1cmwoI2xvYmUtaWNvbnMtY3Vyc29ydW5kZWZpbmVkLWZpbGwtMikiPjwvcGF0aD4KICAgIDxwYXRoIGQ9Ik0yMi4zNSA2TDExLjkyNSAyNFYxMkwyMi4zNSA2eiIgZmlsbD0iIzU1NSI+PC9wYXRoPgogICAgPHBhdGggZD0iTTIyLjM1IDZsLTEwLjQyNSA2TDEuNSA2aDIwLjg1eiIgZmlsbD0iI2ZmZiI+PC9wYXRoPgogICAgPGRlZnM+CiAgICAgICAgPGxpbmVhckdyYWRpZW50IGdyYWRpZW50VW5pdHM9InVzZXJTcGFjZU9uVXNlIiBpZD0ibG9iZS1pY29ucy1jdXJzb3J1bmRlZmluZWQtZmlsbC0wIgogICAgICAgICAgICB4MT0iMTEuOTI1IiB4Mj0iMTEuOTI1IiB5MT0iMTIiIHkyPSIyNCI+CiAgICAgICAgICAgIDxzdG9wIG9mZnNldD0iLjE2IiBzdG9wLWNvbG9yPSIjZmZmIiBzdG9wLW9wYWNpdHk9Ii4zOSI+PC9zdG9wPgogICAgICAgICAgICA8c3RvcCBvZmZzZXQ9Ii42NTgiIHN0b3AtY29sb3I9IiNmZmYiIHN0b3Atb3BhY2l0eT0iLjgiPjwvc3RvcD4KICAgICAgICA8L2xpbmVhckdyYWRpZW50PgogICAgICAgIDxsaW5lYXJHcmFkaWVudCBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSIgaWQ9ImxvYmUtaWNvbnMtY3Vyc29ydW5kZWZpbmVkLWZpbGwtMSIKICAgICAgICAgICAgeDE9IjIyLjM1IiB4Mj0iMTEuOTI1IiB5MT0iNi4wMzciIHkyPSIxMi4xNSI+CiAgICAgICAgICAgIDxzdG9wIG9mZnNldD0iLjE4MiIgc3RvcC1jb2xvcj0iI2ZmZiIgc3RvcC1vcGFjaXR5PSIuMzEiPjwvc3RvcD4KICAgICAgICAgICAgPHN0b3Agb2Zmc2V0PSIuNzE1IiBzdG9wLWNvbG9yPSIjZmZmIiBzdG9wLW9wYWNpdHk9IjAiPjwvc3RvcD4KICAgICAgICA8L2xpbmVhckdyYWRpZW50PgogICAgICAgIDxsaW5lYXJHcmFkaWVudCBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSIgaWQ9ImxvYmUtaWNvbnMtY3Vyc29ydW5kZWZpbmVkLWZpbGwtMiIKICAgICAgICAgICAgeDE9IjExLjkyNSIgeDI9IjEuNSIgeTE9IjAiIHkyPSIxOCI+CiAgICAgICAgICAgIDxzdG9wIHN0b3AtY29sb3I9IiNmZmYiIHN0b3Atb3BhY2l0eT0iLjYiPjwvc3RvcD4KICAgICAgICAgICAgPHN0b3Agb2Zmc2V0PSIuNjY3IiBzdG9wLWNvbG9yPSIjZmZmIiBzdG9wLW9wYWNpdHk9Ii4yMiI+PC9zdG9wPgogICAgICAgIDwvbGluZWFyR3JhZGllbnQ+CiAgICA8L2RlZnM+Cjwvc3ZnPgo=)][cursor-install-basic] [![Verified on MseeP](https://img.shields.io/badge/MseeP-Verified-green)](https://mseep.ai/app/13fce476-0e74-4b1e-ab82-1df2a3204809) [![Trust Score](https://archestra.ai/mcp-catalog/api/badge/quality/Couchbase-Ecosystem/mcp-server-couchbase)](https://archestra.ai/mcp-catalog/couchbase-ecosystem__mcp-server-couchbase)

For full documentation, visit [mcp-server.couchbase.com](https://mcp-server.couchbase.com/).

<a href="https://glama.ai/mcp/servers/@Couchbase-Ecosystem/mcp-server-couchbase">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@Couchbase-Ecosystem/mcp-server-couchbase/badge" alt="Couchbase Server MCP server" />
</a>

<!-- mcp-name: io.github.couchbase/mcp-server-couchbase -->

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

## Prerequisites

- Python 3.10 or higher.
- A running Couchbase cluster. The easiest way to get started is to use [Capella](https://docs.couchbase.com/cloud/get-started/create-account.html#getting-started) free tier, which is fully managed version of Couchbase server. You can follow [instructions](https://docs.couchbase.com/cloud/clusters/data-service/import-data-documents.html#import-sample-data) to import one of the sample datasets or import your own.
- [uv](https://docs.astral.sh/uv/) installed to run the server.
- An [MCP client](https://modelcontextprotocol.io/clients) such as [Claude Desktop](https://claude.ai/download) installed to connect the server to Claude. The instructions are provided for Claude Desktop and Cursor. Other MCP clients could be used as well.

## Configuration

The MCP server can be run either from the prebuilt PyPI package or the source using uv.

### Running from PyPI

We publish a pre built [PyPI package](https://pypi.org/project/couchbase-mcp-server/) for the MCP server.

#### Server Configuration using Pre built Package for MCP Clients

#### Basic Authentication

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "uvx",
      "args": ["couchbase-mcp-server"],
      "env": {
        "CB_CONNECTION_STRING": "couchbases://connection-string",
        "CB_USERNAME": "username",
        "CB_PASSWORD": "password"
      }
    }
  }
}
```

or

#### mTLS

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "uvx",
      "args": ["couchbase-mcp-server"],
      "env": {
        "CB_CONNECTION_STRING": "couchbases://connection-string",
        "CB_CLIENT_CERT_PATH": "/path/to/client-certificate.pem",
        "CB_CLIENT_KEY_PATH": "/path/to/client.key"
      }
    }
  }
}
```

> Note: If you have other MCP servers in use in the client, you can add it to the existing `mcpServers` object.

### Running from Source

The MCP server can be run from the source using this repository.

#### Clone the repository to your local machine

```bash
git clone https://github.com/couchbase/mcp-server-couchbase.git
```

#### Server Configuration using Source for MCP Clients

This is the common configuration for the MCP clients such as Claude Desktop, Cursor, Windsurf Editor.

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "uv",
      "args": [
        "--directory",
        "path/to/cloned/repo/mcp-server-couchbase/",
        "run",
        "src/mcp_server.py"
      ],
      "env": {
        "CB_CONNECTION_STRING": "couchbases://connection-string",
        "CB_USERNAME": "username",
        "CB_PASSWORD": "password"
      }
    }
  }
}
```

> Note: `path/to/cloned/repo/mcp-server-couchbase/` should be the path to the cloned repository on your local machine. Don't forget the trailing slash at the end!

> Note: If you have other MCP servers in use in the client, you can add it to the existing `mcpServers` object.

### Additional Configuration for MCP Server

The server can be configured using environment variables or command line arguments:

| Environment Variable | CLI Argument | Description | Default |
| ---------------------------- | ------------------------ | ------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `CB_CONNECTION_STRING` | `--connection-string` | Connection string to the Couchbase cluster | **Required** |
| `CB_USERNAME` | `--username` | Username with access to required buckets for basic authentication | **Required (or Client Certificate and Key needed for mTLS)** |
| `CB_PASSWORD` | `--password` | Password for basic authentication | **Required (or Client Certificate and Key needed for mTLS)** |
| `CB_CLIENT_CERT_PATH` | `--client-cert-path` | Path to the client certificate file for mTLS authentication | **Required if using mTLS (or Username and Password required)** |
| `CB_CLIENT_KEY_PATH` | `--client-key-path` | Path to the client key file for mTLS authentication | **Required if using mTLS (or Username and Password required)** |
| `CB_CA_CERT_PATH` | `--ca-cert-path` | Path to server root certificate for TLS if server is configured with a self-signed/untrusted certificate. This will not be required if you are connecting to Capella | |
| `CB_MCP_READ_ONLY_MODE` | `--read-only-mode` | Prevent all data modifications (KV and Query). When enabled, KV write tools are not loaded. | `true` |
| `CB_MCP_TRANSPORT` | `--transport` | Transport mode: `stdio`, `http`, `sse` | `stdio` |
| `CB_MCP_HOST` | `--host` | Host for HTTP/SSE transport modes | `127.0.0.1` |
| `CB_MCP_PORT` | `--port` | Port for HTTP/SSE transport modes | `8000` |
| `CB_MCP_DISABLED_TOOLS` | `--disabled-tools` | Tools to disable (see [Disabling Tools](#disabling-tools)) | None |
| `CB_MCP_CONFIRMATION_REQUIRED_TOOLS` | `--confirmation-required-tools` | Tools that require explicit user confirmation before execution via MCP elicitation (see [Elicitation/Confirmation Required Tools](#elicitationconfirmation-for-tool-calls)) | None |
| `CB_MCP_LOG_LEVEL` | `--log-level` | Logging level for the MCP server: `off`, `debug`, `info`, `warning`, `error` (see [Logging](#logging)) | `info` |
| `CB_MCP_LOG_SINKS` | `--log-sinks` | Comma-separated log destinations: `stderr`, `file`, or both (see [Logging](#logging)) | `stderr` |
| `CB_MCP_LOG_FILE` | `--log-file` | Base path for per-level log files (only used when the `file` sink is enabled) | `mcp_server.log` |
| `CB_MCP_LOG_MAX_BYTES` | `--log-max-bytes` | Maximum size in bytes per log file before it rotates | `1048576` (1 MB) |
| `CB_MCP_OAUTH_JWT_JWKS_URI` | `--oauth-jwks-uri` | JWKS endpoint of the identity provider used to verify bearer JWTs. Enables OAuth when set with the issuer and audience (see [OAuth 2.1 Authorization](#oauth-21-authorization)) | None |
| `CB_MCP_OAUTH_JWT_ISSUER` | `--oauth-issuer` | Expected JWT `iss` claim. Required to enable OAuth | None |
| `CB_MCP_OAUTH_JWT_AUDIENCE` | `--oauth-audience` | Expected JWT `aud` claim. Required to enable OAuth | None |
| `CB_MCP_OAUTH_JWT_ALGORITHM` | `--oauth-algorithm` | JWT signing algorithm: one of `RS256/384/512`, `ES256/384/512`, `PS256/384/512` | `RS256` |
| `CB_MCP_OAUTH_MCP_BASE_URL` | `--oauth-mcp-base-url` | Public base URL of this server. When set, publishes RFC 9728 Protected Resource Metadata so PRM-aware clients can discover the IdP | None |

#### Read-Only Mode Configuration

**`CB_MCP_READ_ONLY_MODE`** is the single switch controlling write operations:

- When `true` (default): All write operations (KV and Query) are disabled. KV write tools (upsert, insert, replace, delete) are **not loaded** and will not be available to the LLM, and SQL++ queries that modify data or structure are blocked.
- When `false`: KV write tools are loaded and SQL++ data/structure modification queries are allowed.

This is the recommended safe default to prevent inadvertent data modifications by LLMs.

> Note: For authentication, you need either the Username and Password or the Client Certificate and key paths. Optionally, you can specify the CA root certificate path that will be used to validate the server certificates.
> If both the Client Certificate & key path and the username and password are specified, the client certificates will be used for authentication.

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
      "command": "uvx",
      "args": ["couchbase-mcp-server"],
      "env": {
        "CB_CONNECTION_STRING": "couchbases://connection-string",
        "CB_USERNAME": "username",
        "CB_PASSWORD": "password",
        "CB_MCP_DISABLED_TOOLS": "upsert_document_by_id,delete_document_by_id"
      }
    }
  }
}
```

**Using file path (recommended for many tools):**

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "uvx",
      "args": ["couchbase-mcp-server"],
      "env": {
        "CB_CONNECTION_STRING": "couchbases://connection-string",
        "CB_USERNAME": "username",
        "CB_PASSWORD": "password",
        "CB_MCP_DISABLED_TOOLS": "/path/to/disabled_tools.txt"
      }
    }
  }
}
```

#### Important Security Note

> **Warning:** Disabling tools alone does not guarantee that certain operations cannot be performed. The underlying database user's RBAC (Role-Based Access Control) permissions are the authoritative security control.
>
> For example, even if you disable `upsert_document_by_id` and `delete_document_by_id`, data modifications can still occur via the `run_sql_plus_plus_query` tool using SQL++ DML statements (INSERT, UPDATE, DELETE, MERGE) unless:
>
> - The `CB_MCP_READ_ONLY_MODE` is set to `true` (default), OR
> - The database user lacks the necessary RBAC permissions for data modification
>
> **Best Practice:** Always configure appropriate RBAC permissions on your Couchbase user credentials as the primary security measure. Use tool disabling as an additional layer to guide LLM behavior and reduce the attack surface, not as the sole security control.

### Elicitation/Confirmation for Tool Calls

You can require explicit user confirmation for specific tools before execution (when the MCP client supports elicitation).

`CB_MCP_CONFIRMATION_REQUIRED_TOOLS` / `--confirmation-required-tools` supports these formats:

- Comma-separated list
- File path (one tool name per line, `#` comments supported)

**Example:**

```bash
# Environment variable
CB_MCP_CONFIRMATION_REQUIRED_TOOLS="delete_document_by_id,replace_document_by_id"

# Command line
uvx couchbase-mcp-server --confirmation-required-tools delete_document_by_id,replace_document_by_id
```

When a listed tool is invoked:

- If the client supports elicitation, the user is prompted to confirm.
- If the client does not support elicitation, the tool executes without confirmation for backward compatibility.

You can also check the version of the server using:

```bash
uvx couchbase-mcp-server --version
```

### Logging

The MCP server logs to `stderr` by default. Logging is configured with the `CB_MCP_LOG_*` variables listed in [Additional Configuration](#additional-configuration-for-mcp-server):

- **`CB_MCP_LOG_LEVEL`** — how much is logged: `info` (the default) logs lifecycle events and tool invocations, `debug` adds verbose internal detail, and `off` disables all logging.
- **`CB_MCP_LOG_SINKS`** — where logs go: `stderr` (the default), per-level rotating files (`file`), or both. With `file`, one file is written per level (for example `mcp_server.info.log` and `mcp_server.error.log`) at the path set by `CB_MCP_LOG_FILE`.

```bash
# Enable debug logging to both stderr and rotating per-level files
uvx couchbase-mcp-server --log-level=debug --log-sinks=stderr,file
```

For more details, see the [documentation](https://mcp-server.couchbase.com/configuration/logging).

### Client Specific Configuration

<details>
<summary>Claude Desktop</summary>

Follow the steps below to use Couchbase MCP server with Claude Desktop MCP client

1. The MCP server can now be added to Claude Desktop by editing the configuration file. More detailed instructions can be found on the [MCP quickstart guide](https://modelcontextprotocol.io/quickstart/user).

   - On Mac, the configuration file is located at `~/Library/Application Support/Claude/claude_desktop_config.json`
   - On Windows, the configuration file is located at `%APPDATA%\Claude\claude_desktop_config.json`

   Open the configuration file and add the [configuration](#configuration) to the `mcpServers` section.

2. Restart Claude Desktop to apply the changes.

3. You can now use the server in Claude Desktop to run queries on the Couchbase cluster using natural language and perform CRUD operations on documents.

Logs

The logs for Claude Desktop can be found in the following locations:

- MacOS: ~/Library/Logs/Claude
- Windows: %APPDATA%\Claude\Logs

The logs can be used to diagnose connection issues or other problems with your MCP server configuration. For more details, refer to the [official documentation](https://modelcontextprotocol.io/quickstart/user#troubleshooting).

</details>

<details>
<summary>Cursor</summary>

Follow steps below to use Couchbase MCP server with Cursor:

1. Install [Cursor](https://cursor.sh/) on your machine.

2. In Cursor, go to Cursor > Cursor Settings > Tools & Integrations > MCP Tools. Also, checkout the docs on [setting up MCP server configuration](https://docs.cursor.com/en/context/mcp#configuring-mcp-servers) from Cursor.

3. Specify the same [configuration](#configuration) manually, or use the one-click [Install in Cursor][cursor-install-basic] link. You may need to add the server configuration under a parent key of `mcpServers`.

   > Note: The install link uses placeholder values from the configuration examples above. Update the connection string and credentials after installation.

4. Save the configuration.

5. You will see couchbase as an added server in MCP servers list. Refresh to see if server is enabled.

6. You can now use the Couchbase MCP server in Cursor to query your Couchbase cluster using natural language and perform CRUD operations on documents.

[cursor-install-basic]: https://cursor.com/en-US/install-mcp?name=Couchbase&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJjb3VjaGJhc2UtbWNwLXNlcnZlciJdLCJlbnYiOnsiQ0JfQ09OTkVDVElPTl9TVFJJTkciOiJjb3VjaGJhc2VzOi8vY29ubmVjdGlvbi1zdHJpbmciLCJDQl9VU0VSTkFNRSI6InVzZXJuYW1lIiwiQ0JfUEFTU1dPUkQiOiJwYXNzd29yZCJ9fQ%3D%3D

For more details about MCP integration with Cursor, refer to the [official Cursor MCP documentation](https://docs.cursor.com/en/context/mcp).

Logs

In the bottom panel of Cursor, click on "Output" and select "Cursor MCP" from the dropdown menu to view server logs. This can help diagnose connection issues or other problems with your MCP server configuration.

</details>

<details>
<summary>Windsurf Editor</summary>

Follow the steps below to use the Couchbase MCP server with [Windsurf Editor](https://windsurf.com/).

1. Install [Windsurf Editor](https://windsurf.com/download) on your machine.

2. In Windsurf Editor, navigate to Command Palette > Windsurf MCP Configuration Panel or Windsurf - Settings > Advanced > Cascade > Model Context Protocol (MCP) Servers. For more details on the configuration, please refer to the [official documentation](https://docs.windsurf.com/windsurf/cascade/mcp#adding-a-new-mcp-plugin).

3. Click on Add Server and then Add custom server. On the configuration that opens in the editor, add the Couchbase MCP Server [configuration](#configuration) from above.

4. Save the configuration.

5. You will see couchbase as an added server in MCP Servers list under Advanced Settings. Refresh to see if server is enabled.

6. You can now use the Couchbase MCP server in Windsurf Editor to query your Couchbase cluster using natural language and perform CRUD operations on documents.

For more details about MCP integration with Windsurf Editor, refer to the official [Windsurf MCP documentation](https://docs.windsurf.com/windsurf/cascade/mcp).

</details>

<details>
<summary>VS Code</summary>

Follow the steps below to use the Couchbase MCP server with [VS Code](https://code.visualstudio.com/).

1. Install [VS Code](https://code.visualstudio.com/)
2. Following are a couple of ways to configure the MCP server.
    - For a Workspace server configuration
      - Create a new file in workspace as .vscode/mcp.json.
      - Add the [configuration](#configuration) and save the file.
    - For the Global server configuration:
      - Run **MCP: Open User Configuration** in the Command Palette (`Ctrl+Shift+P` or `Cmd+Shift+P`)
      - Add the [configuration](#configuration) and save the file.
    - **Note**: VS Code uses `servers` as the top-level JSON property in mcp.json files to define MCP (Model Context Protocol) servers, while Cursor uses `mcpServers` for the equivalent configuration. Check the [VS Code client configurations](https://code.visualstudio.com/docs/copilot/customization/mcp-servers) for any further changes or details. An example VS Code configuration is provided below.

      ```json
        {
          "servers": {
            "couchbase": {
              "command": "uvx",
              "args": ["couchbase-mcp-server"],
              "env": {
                "CB_CONNECTION_STRING": "couchbases://connection-string",
                "CB_USERNAME": "username",
                "CB_PASSWORD": "password"
              }
            }
          }
        }
        ```

3. Once you save the file, the server starts and a small action list appears with `Running|Stop|n Tools|More..`.
4. Click on the options from the option list to `Start`/`Stop`/manage the server.
5. You can now use the Couchbase MCP server in VS Code to query your Couchbase cluster using natural language and perform CRUD operations on documents.

Logs:
In the Command Palette (`Ctrl+Shift+P` or `Cmd+Shift+P`),

- run **MCP: List Servers** command and pick the couchbase server
- choose “Show Output” to see its logs in the Output tab.

</details>

<details>
<summary>JetBrains IDEs</summary>

Follow the steps below to use the Couchbase MCP server with [JetBrains IDEs](https://www.jetbrains.com/)

1. Install any one of the [JetBrains IDEs](https://www.jetbrains.com/)
2. Install any one of the JetBrains plugins - [AI Assistant](https://www.jetbrains.com/help/ai-assistant/getting-started-with-ai-assistant.html) or [Junie](https://www.jetbrains.com/help/junie/get-started-with-junie.html)
3. Navigate to **Settings > Tools > AI Assistant or Junie > MCP Server**
4. Click "+" to add the Couchbase MCP [configuration](#configuration) and click Save.
5. You will see the Couchbase MCP server added to the list of servers. Once you click Apply, the Couchbase MCP server starts and on-hover of status, it shows all the tools available.
6. You can now use the Couchbase MCP server in JetBrains IDEs to query your Couchbase cluster using natural language and perform CRUD operations on documents.

Logs:
The log file can be explored at **Help > Show Log in Finder (Explorer) > mcp > couchbase**

</details>

## Streamable HTTP Transport Mode

The MCP Server can be run in [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http) transport mode which allows multiple clients to connect to the same server instance via HTTP.
Check if your [MCP client](https://modelcontextprotocol.io/clients) supports streamable http transport before attempting to connect to MCP server in this mode.

> Note: OAuth 2.1 authorization is supported on this transport. See [OAuth 2.1 Authorization](#oauth-21-authorization). Without OAuth configured, the HTTP endpoint is unauthenticated.

### Usage

By default, the MCP server will run on port 8000 but this can be configured using the `--port` or `CB_MCP_PORT` environment variable.

```bash
uvx couchbase-mcp-server \
  --connection-string='<couchbase_connection_string>' \
  --username='<database_username>' \
  --password='<database_password>' \
  --read-only-mode=true \
  --transport=http
```

The server will be available on <http://localhost:8000/mcp>. This can be used in MCP clients supporting streamable http transport mode such as Cursor.

### MCP Client Configuration

```json
{
  "mcpServers": {
    "couchbase-http": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

## SSE Transport Mode

There is an option to run the MCP server in [Server-Sent Events (SSE)](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports#http-with-sse) transport mode.

> Note: SSE mode has been [deprecated](https://modelcontextprotocol.io/docs/concepts/transports#server-sent-events-sse-deprecated) by MCP. We have support for [Streamable HTTP](#streamable-http-transport-mode).

### SSE: Usage

By default, the MCP server will run on port 8000 but this can be configured using the `--port` or `CB_MCP_PORT` environment variable.

```bash
uvx couchbase-mcp-server \
  --connection-string='<couchbase_connection_string>' \
  --username='<database_username>' \
  --password='<database_password>' \
  --read-only-mode=true \
  --transport=sse
```

The server will be available on <http://localhost:8000/sse>. This can be used in MCP clients supporting SSE transport mode such as Cursor.

### SSE: MCP Client Configuration

```json
{
  "mcpServers": {
    "couchbase-sse": {
      "url": "http://localhost:8000/sse"
    }
  }
}
```

## OAuth 2.1 Authorization

When running with `--transport=http`, the MCP server can act as an **OAuth 2.1 resource server**: it validates incoming bearer JWTs against your identity provider's JWKS. It is provider-agnostic (any OAuth 2.1 / OIDC provider that publishes a JWKS — Auth0, Okta, Keycloak, AWS Cognito, Microsoft Entra, etc.) and does **not** issue tokens or manage users. OAuth settings are ignored on `stdio`.

OAuth is configured with the `CB_MCP_OAUTH_*` variables listed in [Additional Configuration](#additional-configuration-for-mcp-server):

- OAuth activates only when all three of `CB_MCP_OAUTH_JWT_JWKS_URI`, `CB_MCP_OAUTH_JWT_ISSUER`, and `CB_MCP_OAUTH_JWT_AUDIENCE` are set; setting only some of them fails at startup.
- Setting `CB_MCP_OAUTH_MCP_BASE_URL` additionally publishes RFC 9728 Protected Resource Metadata so PRM-aware clients can discover the authorization server.
- Access is gated by two scopes read from the token's `scope`/`scp` claim: `couchbase-mcp:read` (read tools, including SQL++) and `couchbase-mcp:write` (KV mutation tools). Full access requires both.

```bash
uvx couchbase-mcp-server \
  --connection-string='<couchbase_connection_string>' \
  --username='<database_username>' \
  --password='<database_password>' \
  --transport=http \
  --oauth-jwks-uri='https://auth.example.com/.well-known/jwks.json' \
  --oauth-issuer='https://auth.example.com/' \
  --oauth-audience='couchbase-mcp-server' \
  --oauth-mcp-base-url='<public_base_url_of_this_server>'
```

For full details, see the [documentation](https://mcp-server.couchbase.com/configuration/oauth).

## Docker Image

The MCP server can also be built and run as a Docker container. Prebuilt images can be found on [DockerHub](https://hub.docker.com/r/couchbase/mcp-server) or pulled via `docker pull docker.io/couchbase/mcp-server:latest`.

Alternatively, we are part of the [Docker MCP Catalog](https://hub.docker.com/mcp/server/couchbase/overview).

### Building Image

```bash
docker build -t mcp/couchbase-src .
```

<details>
<summary>Building with Arguments</summary>
If you want to build with the build arguments for commit hash and the build time, you can build using:

```bash
docker build --build-arg GIT_COMMIT_HASH=$(git rev-parse HEAD) \
  --build-arg BUILD_DATE=$(date -u +'%Y-%m-%dT%H:%M:%SZ') \
  -t mcp/couchbase-src .
```

**Alternatively, use the provided build script:**

```bash
# Build with default image name (mcp/couchbase-src)
./build.sh

# Build with custom image name
./build.sh my-custom/image-name
```

This script automatically:

- Accepts an optional image name parameter (defaults to `mcp/couchbase-src`)
- Generates git commit hash and build timestamp
- Creates multiple useful tags (`latest`, `<short-commit>`)
- Shows build information and results
- Uses the same arguments as CI/CD builds

**Verify image labels:**

```bash
# View git commit hash in image
docker inspect --format='{{index .Config.Labels "org.opencontainers.image.revision"}}' mcp/couchbase-src:latest

# View all metadata labels
docker inspect --format='{{json .Config.Labels}}' mcp/couchbase-src:latest
```

</details>

### Running

The MCP server can be run with the environment variables being used to configure the Couchbase settings. The environment variables are the same as described in the [Additional Configuration section](#additional-configuration-for-mcp-server).

#### Independent Docker Container

```bash
docker run --rm -i \
  -e CB_CONNECTION_STRING='<couchbase_connection_string>' \
  -e CB_USERNAME='<database_user>' \
  -e CB_PASSWORD='<database_password>' \
  -e CB_MCP_TRANSPORT='<http|sse|stdio>' \
  -e CB_MCP_READ_ONLY_MODE='<true|false>' \
  -e CB_MCP_CONFIRMATION_REQUIRED_TOOLS='delete_document_by_id' \
  -e CB_MCP_PORT=9001 \
  -e CB_MCP_HOST=0.0.0.0 \
  -p 9001:9001 \
  mcp/couchbase-src
```

The `CB_MCP_PORT` and `CB_MCP_HOST` environment variables are only applicable in the case of HTTP transport modes like http and sse.

#### Docker: MCP Client Configuration

The Docker image can be used in `stdio` transport mode with the following configuration.

```json
{
  "mcpServers": {
    "couchbase-mcp-docker": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-e",
        "CB_CONNECTION_STRING=<couchbase_connection_string>",
        "-e",
        "CB_USERNAME=<database_user>",
        "-e",
        "CB_PASSWORD=<database_password>",
        "mcp/couchbase-src"
      ]
    }
  }
}
```

Notes

- The `couchbase_connection_string` value depends on whether the Couchbase server is running on the same host machine, in another Docker container, or on a remote host. If your Couchbase server is running on your host machine, your connection string would likely be of the form `couchbase://host.docker.internal`. For details refer to the [docker documentation](https://docs.docker.com/desktop/features/networking/#i-want-to-connect-from-a-container-to-a-service-on-the-host).
- You can specify the container's networking using the `--network=<your_network>` option. The network you choose depends on your environment; the default is `bridge`. For details, refer to [network drivers in docker](https://docs.docker.com/engine/network/drivers/).

### Risks Associated with LLMs

- The use of large language models and similar technology involves risks, including the potential for inaccurate or harmful outputs.
- Couchbase does not review or evaluate the quality or accuracy of such outputs, and such outputs may not reflect Couchbase's views.
- You are solely responsible for determining whether to use large language models and related technology, and for complying with any license terms, terms of use, and your organization's policies governing your use of the same.

## Troubleshooting Tips

- Ensure the path to your MCP server repository is correct in the configuration if running from source.
- Verify that your Couchbase connection string, database username, password or the path to the certificates are correct.
- If using Couchbase Capella, ensure that the cluster is [accessible](https://docs.couchbase.com/cloud/clusters/allow-ip-address.html) from the machine where the MCP server is running.
- Check that the database user has proper permissions to access at least one bucket.
- Confirm that the `uv` package manager is properly installed and accessible. You may need to provide absolute path to `uv`/`uvx` in the `command` field in the configuration.
- Check the logs for any errors or warnings that may indicate issues with the MCP server. The location of the logs depend on your MCP client.
- If you are observing issues running your MCP server from source after updating your local MCP server repository, try running `uv sync` to update the [dependencies](https://docs.astral.sh/uv/concepts/projects/sync/#syncing-the-environment).

## Integration testing

We provide high-level MCP integration tests to verify that the server exposes the expected tools and that they can be invoked against a demo Couchbase cluster.

1. Export demo cluster credentials:
   - `CB_CONNECTION_STRING`
   - `CB_USERNAME`
   - `CB_PASSWORD`
   - Optional: `CB_MCP_TEST_BUCKET` (a bucket to probe during the tests)
2. Run the tests:

```bash
uv run pytest tests/ -v
```

---

## 👩‍💻 Contributing

We welcome contributions from the community! Whether you want to fix bugs, add features, or improve documentation, your help is appreciated.

If you need help, have found a bug, or want to contribute improvements, the best place to do that is right here — by [opening a GitHub issue](https://github.com/couchbase/mcp-server-couchbase/issues).

### For Developers

If you're interested in contributing code or setting up a development environment:

📖 **See [CONTRIBUTING.md](CONTRIBUTING.md)** for comprehensive developer setup instructions, including:

- Development environment setup with `uv`
- Code linting and formatting with Ruff
- Pre-commit hooks installation
- Project structure overview
- Development workflow and practices

### Quick Start for Contributors

```bash
# Clone and setup
git clone https://github.com/couchbase/mcp-server-couchbase.git
cd mcp-server-couchbase

# Install with development dependencies
uv sync --extra dev

# Install pre-commit hooks
uv run pre-commit install

# Run linting
./scripts/lint.sh
```

---

## 📢 Support Policy

We truly appreciate your interest in this project!
This project is **Couchbase community-maintained**, which means it's **not officially supported** by our support team. However, our engineers are actively monitoring and maintaining this repo and will try to resolve issues on a best-effort basis.

Our support portal is unable to assist with requests related to this project, so we kindly ask that all inquiries stay within GitHub.

Your collaboration helps us all move forward together — thank you!
