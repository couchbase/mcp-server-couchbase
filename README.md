# Couchbase MCP Server

Couchbase MCP Server is a self-hosted [Model Context Protocol (MCP)](https://modelcontextprotocol.io/docs/2026-07-28/getting-started/intro) server that connects AI agents and assistants — Claude, Cursor, Windsurf, VS Code Copilot, JetBrains AI Assistant and any other [MCP client](https://modelcontextprotocol.io/clients) — to data in Couchbase Server clusters, on [Capella](https://www.couchbase.com/products/capella/) or self-managed, and in self-managed Operational Insights clusters. An agent can inspect your cluster, explore schemas, run SQL++, full-text and vector queries, read and write documents, and diagnose query and cluster performance in natural language.

It is read-only by default, supports STDIO and Streamable HTTP (with OAuth 2.1), and is distributed on [PyPI](https://pypi.org/project/couchbase-mcp-server/) and [Docker Hub](https://hub.docker.com/r/couchbase/mcp-server). Enterprise support is available by licensing [Couchbase AI Data Plane](https://www.couchbase.com/downloads/?family=ai-data-plane), which also entitles use and enterprise support of Couchbase Agent Memory and Couchbase Agent Catalog.

[![Docs](https://img.shields.io/badge/Docs-1B9E5A?logo=docusaurus&logoColor=white)](https://docs.couchbase.com/mcp-server/get-started/overview.html) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/) [![PyPI version](https://badge.fury.io/py/couchbase-mcp-server.svg)](https://pypi.org/project/couchbase-mcp-server/) [![Install in Cursor](https://img.shields.io/badge/Cursor-Install_Server-1e1e1e?logo=data:image/svg%2bxml;base64,PHN2ZyBoZWlnaHQ9IjFlbSIgc3R5bGU9ImZsZXg6bm9uZTtsaW5lLWhlaWdodDoxIiB2aWV3Qm94PSIwIDAgMjQgMjQiIHdpZHRoPSIxZW0iCiAgICB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPgogICAgPHRpdGxlPkN1cnNvcjwvdGl0bGU+CiAgICA8cGF0aCBkPSJNMTEuOTI1IDI0bDEwLjQyNS02LTEwLjQyNS02TDEuNSAxOGwxMC40MjUgNnoiCiAgICAgICAgZmlsbD0idXJsKCNsb2JlLWljb25zLWN1cnNvcnVuZGVmaW5lZC1maWxsLTApIj48L3BhdGg+CiAgICA8cGF0aCBkPSJNMjIuMzUgMThWNkwxMS45MjUgMHYxMmwxMC40MjUgNnoiIGZpbGw9InVybCgjbG9iZS1pY29ucy1jdXJzb3J1bmRlZmluZWQtZmlsbC0xKSI+PC9wYXRoPgogICAgPHBhdGggZD0iTTExLjkyNSAwTDEuNSA2djEybDEwLjQyNS02VjB6IiBmaWxsPSJ1cmwoI2xvYmUtaWNvbnMtY3Vyc29ydW5kZWZpbmVkLWZpbGwtMikiPjwvcGF0aD4KICAgIDxwYXRoIGQ9Ik0yMi4zNSA2TDExLjkyNSAyNFYxMkwyMi4zNSA2eiIgZmlsbD0iIzU1NSI+PC9wYXRoPgogICAgPHBhdGggZD0iTTIyLjM1IDZsLTEwLjQyNSA2TDEuNSA2aDIwLjg1eiIgZmlsbD0iI2ZmZiI+PC9wYXRoPgogICAgPGRlZnM+CiAgICAgICAgPGxpbmVhckdyYWRpZW50IGdyYWRpZW50VW5pdHM9InVzZXJTcGFjZU9uVXNlIiBpZD0ibG9iZS1pY29ucy1jdXJzb3J1bmRlZmluZWQtZmlsbC0wIgogICAgICAgICAgICB4MT0iMTEuOTI1IiB4Mj0iMTEuOTI1IiB5MT0iMTIiIHkyPSIyNCI+CiAgICAgICAgICAgIDxzdG9wIG9mZnNldD0iLjE2IiBzdG9wLWNvbG9yPSIjZmZmIiBzdG9wLW9wYWNpdHk9Ii4zOSI+PC9zdG9wPgogICAgICAgICAgICA8c3RvcCBvZmZzZXQ9Ii42NTgiIHN0b3AtY29sb3I9IiNmZmYiIHN0b3Atb3BhY2l0eT0iLjgiPjwvc3RvcD4KICAgICAgICA8L2xpbmVhckdyYWRpZW50PgogICAgICAgIDxsaW5lYXJHcmFkaWVudCBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSIgaWQ9ImxvYmUtaWNvbnMtY3Vyc29ydW5kZWZpbmVkLWZpbGwtMSIKICAgICAgICAgICAgeDE9IjIyLjM1IiB4Mj0iMTEuOTI1IiB5MT0iNi4wMzciIHkyPSIxMi4xNSI+CiAgICAgICAgICAgIDxzdG9wIG9mZnNldD0iLjE4MiIgc3RvcC1jb2xvcj0iI2ZmZiIgc3RvcC1vcGFjaXR5PSIuMzEiPjwvc3RvcD4KICAgICAgICAgICAgPHN0b3Agb2Zmc2V0PSIuNzE1IiBzdG9wLWNvbG9yPSIjZmZmIiBzdG9wLW9wYWNpdHk9IjAiPjwvc3RvcD4KICAgICAgICA8L2xpbmVhckdyYWRpZW50PgogICAgICAgIDxsaW5lYXJHcmFkaWVudCBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSIgaWQ9ImxvYmUtaWNvbnMtY3Vyc29ydW5kZWZpbmVkLWZpbGwtMiIKICAgICAgICAgICAgeDE9IjExLjkyNSIgeDI9IjEuNSIgeTE9IjAiIHkyPSIxOCI+CiAgICAgICAgICAgIDxzdG9wIHN0b3AtY29sb3I9IiNmZmYiIHN0b3Atb3BhY2l0eT0iLjYiPjwvc3RvcD4KICAgICAgICAgICAgPHN0b3Agb2Zmc2V0PSIuNjY3IiBzdG9wLWNvbG9yPSIjZmZmIiBzdG9wLW9wYWNpdHk9Ii4yMiI+PC9zdG9wPgogICAgICAgIDwvbGluZWFyR3JhZGllbnQ+CiAgICA8L2RlZnM+Cjwvc3ZnPgo=)][cursor-install-basic] [![Verified on MseeP](https://img.shields.io/badge/MseeP-Verified-green)](https://mseep.ai/app/13fce476-0e74-4b1e-ab82-1df2a3204809) [![Trust Score](https://archestra.ai/mcp-catalog/api/badge/quality/Couchbase-Ecosystem/mcp-server-couchbase)](https://archestra.ai/mcp-catalog/couchbase-ecosystem__mcp-server-couchbase)

Full documentation: [docs.couchbase.com/mcp-server](https://docs.couchbase.com/mcp-server/get-started/overview.html)

<a href="https://glama.ai/mcp/servers/@couchbase/mcp-server-couchbase">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@couchbase/mcp-server-couchbase/badge" alt="Couchbase Server MCP server" />
</a>

<!-- mcp-name: io.github.couchbase/mcp-server-couchbase -->

## Table of Contents

- [Why Couchbase MCP Server](#why-couchbase-mcp-server)
- [Choosing a Server](#choosing-a-server)
- [Operational Server](#operational-server)
  - [Quick Start](#operational-quick-start)
  - [Connection and Authentication](#operational-connection-and-authentication)
  - [Tools](#operational-tools)
  - [Embedding Providers](#embedding-providers)
  - [All Options](#all-options)
- [Operational Insights Server](#operational-insights-server)
  - [Quick Start](#operational-insights-quick-start)
  - [Connection and Authentication](#operational-insights-connection-and-authentication)
  - [Tools](#operational-insights-tools)
  - [Exporting Large Results](#exporting-large-results)
- [Common Configuration](#common-configuration)
  - [Safety Controls](#safety-controls)
  - [Transports](#transports)
  - [OAuth 2.1 Authorization](#oauth-21-authorization)
  - [Logging](#logging)
  - [Auditing](#auditing)
- [Client Setup](#client-setup)
- [Docker](#docker)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Contributing](#contributing)
- [Usage Data, LLM Risks and Support Policy](#usage-data-llm-risks-and-support-policy)

## Why Couchbase MCP Server

- **Safe by default** — write tools are not loaded and data-modifying queries are blocked unless you set `CB_MCP_READ_ONLY_MODE=false`; individual tools can also be disabled or gated behind user confirmation.
- **Couchbase Server and Operational Insights** — the [operational server](#operational-server) connects to Couchbase Capella or a self-managed Couchbase Server cluster with the same configuration; the [Operational Insights server](#operational-insights-server) connects to self-managed Operational Insights clusters.
- **RBAC-aware** — tool controls guide the LLM; the Couchbase user's role-based access control remains the authoritative security boundary.
- **Production transports** — STDIO for local desktop clients, or Streamable HTTP with optional OAuth 2.1 (any JWKS-publishing IdP: Auth0, Okta, Keycloak, Entra, Cognito, …), health checks and audit logging for shared deployments. The operational server can also run [multiple workers](#multiple-workers) to use more CPU cores.
- **Any MCP client** — tested with Claude Desktop, Cursor, Windsurf, VS Code and JetBrains AI Assistant/Junie.

Once connected, you can ask things like:

- "What buckets, scopes and collections do I have, and what's the schema of the `orders` collection?"
- "Find the 10 most recent documents in `users` where `status = 'active'`."
- "What are the 5 slowest queries in the last hour, and are any of them missing a covering index?"
- "Is this cluster healthy, and which services are running?"
- "Insert a new document into `products` with these fields: …" (requires `CB_MCP_READ_ONLY_MODE=false`)

## Choosing a Server

This package ships two MCP servers. Each one is started by its own subcommand, connects to a different kind of cluster, and has its own tools. Pick the one that matches your cluster, follow its section for setup and tools, then use [Common Configuration](#common-configuration) for the settings both share. To use both, register each as a separate server in your MCP client.

| | [Operational server](#operational-server) | [Operational Insights server](#operational-insights-server) |
| --- | --- | --- |
| Use it with | A Couchbase cluster (Capella or self-managed) | A self-managed [Operational Insights](https://docs.couchbase.com/operational-insights/current/intro/intro.html) cluster (not available on Capella) |
| Subcommand | `operational` (the default) | `operational-insights` |
| Connection string | `couchbase://` / `couchbases://` | **HTTP(S) URL**, e.g. `http://localhost:8095` |
| Credential variables | `CB_CONNECTION_STRING`, `CB_USERNAME`, … | `CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING`, `CB_OPERATIONAL_INSIGHTS_USERNAME`, … |
| Tools | 52 — cluster health, schema, KV, SQL++, indexes, full-text and vector search, query performance | 13 — schema, SQL++ (sync and async), indexes, exports |
| Default HTTP port | `8000` | `8001` |
| Default log file | `mcp_server.log` | `mcp_server_operational_insights.log` |

> The subcommand is optional for the operational server today — a bare `couchbase-mcp-server` starts it — but naming it keeps your configuration unambiguous. Options go after the subcommand: `couchbase-mcp-server operational --transport=http`. Check the installed version with `uvx couchbase-mcp-server --version`.

## Operational Server

The `operational` server connects to a Couchbase cluster on Capella or self-managed Couchbase Server, using the `couchbase` Python SDK.

### Operational Quick Start

You need a running Couchbase cluster — the [Capella free tier](https://docs.couchbase.com/cloud/get-started/create-account.html#getting-started) is the quickest way to get one (you can [import a sample dataset](https://docs.couchbase.com/cloud/clusters/data-service/import-data-documents.html#import-sample-data)) — and an [MCP client](https://modelcontextprotocol.io/clients). Add **one** of these configurations to your client (see [Client Setup](#client-setup) for where each client keeps it). If the client already has other servers, add `couchbase` to the existing `mcpServers` object.

**From PyPI with uvx** (requires [uv](https://docs.astral.sh/uv/) and Python 3.10+):

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "uvx",
      "args": ["couchbase-mcp-server", "operational"],
      "env": {
        "CB_CONNECTION_STRING": "couchbases://connection-string",
        "CB_USERNAME": "username",
        "CB_PASSWORD": "password"
      }
    }
  }
}
```

**With Docker** (requires only Docker):

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

<details>
<summary>From source</summary>

```bash
git clone https://github.com/couchbase/mcp-server-couchbase.git
```

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "uv",
      "args": [
        "--directory", "path/to/cloned/repo/mcp-server-couchbase/",
        "run", "src/mcp_server.py", "operational"
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

`path/to/cloned/repo/mcp-server-couchbase/` is the path to your clone — keep the trailing slash. After pulling updates, run `uv sync` to update dependencies.

</details>

### Operational Connection and Authentication

Use **either** a username and password **or** a client certificate (mTLS). If both are set, the certificate is used.

| Environment Variable | CLI Argument | Description |
| --- | --- | --- |
| `CB_CONNECTION_STRING` | `--connection-string` | Connection string to the cluster. **Required** |
| `CB_USERNAME` / `CB_PASSWORD` | `--username` / `--password` | Basic authentication |
| `CB_CLIENT_CERT_PATH` / `CB_CLIENT_KEY_PATH` | `--client-cert-path` / `--client-key-path` | mTLS authentication |
| `CB_CA_CERT_PATH` | `--ca-cert-path` | Root certificate for a self-signed/untrusted server certificate (not needed for Capella) |

<details>
<summary>mTLS example</summary>

```json
{
  "mcpServers": {
    "couchbase": {
      "command": "uvx",
      "args": ["couchbase-mcp-server", "operational"],
      "env": {
        "CB_CONNECTION_STRING": "couchbases://connection-string",
        "CB_CLIENT_CERT_PATH": "/path/to/client-certificate.pem",
        "CB_CLIENT_KEY_PATH": "/path/to/client.key"
      }
    }
  }
}
```

</details>

### Operational Tools

Write tools (marked ✏️) are **not loaded** while [read-only mode](#read-only-mode) is on, the default. Tools marked 🖥️ use admin REST endpoints that Capella does not expose: they need **self-managed Couchbase Server 7.6+** and are [not registered on Capella connections](#tools-disabled-automatically-on-capella).

#### Cluster setup and health

| Tool | Description |
| --- | --- |
| `get_server_configuration_status` | Server status and configuration without connecting to the cluster: read-only mode, disabled/confirmation-required tools, OAuth, logging and audit counters |
| `test_cluster_connection` | Check the cluster credentials by connecting to the cluster |
| `get_cluster_health_and_services` | Cluster health and running services, optionally filtered via `service_types` |
| `get_client_connectivity_report` | The SDK's cached connection diagnostics, without active network probing |
| `get_cluster_metrics` 🖥️ | Cluster statistics over a historic time window (stats-range endpoint) |
| `get_cluster_tasks` 🖥️ | Tasks running right now — rebalance, compaction, XDCR, index build. Needs `ro_admin` |
| `get_cluster_health_snapshot` 🖥️ | Per-node health: service topology, membership, orchestrator and a health rollup. Needs `ro_admin` |
| `get_bucket_config` 🖥️ | A bucket's (or every bucket's) configuration and live state — quotas, eviction, replicas, durability, storage, compaction. Needs `ro_admin` |
| `get_cluster_system_events` 🖥️ | The system event log (config changes, failovers, rebalances, restarts), windowed with `since_time`. Needs Full Admin or Cluster Admin |
| `discover_tool_input_values` | Look up exact input values other tools need (e.g. every metric name for `get_cluster_metrics`). Works offline |

#### Data model and schema discovery

| Tool | Description |
| --- | --- |
| `get_buckets_in_cluster` | List all buckets in the cluster |
| `get_scopes_in_bucket` | List all scopes in a bucket |
| `get_collections_in_scope` | List all collections in a scope (requires the Query service) |
| `get_scopes_and_collections_in_bucket` | List all scopes and collections in a bucket |
| `get_schema_for_collection` | Infer the structure of a collection |
| `create_scope` ✏️ | Create a scope in a bucket (Server 7.6+ and Capella) |
| `create_collection` ✏️ | Create a collection in an existing scope (Server 7.6+ and Capella) |
| `delete_scope` ✏️ | Delete a scope and all its collections — permanent |
| `delete_collection` ✏️ | Delete a collection and all its documents — permanent |

#### Document (KV) operations

| Tool | Description |
| --- | --- |
| `get_document_by_id` | Get a document by ID |
| `lookup_subdocument` | Read specific paths, existence checks or counts without fetching the whole document |
| `upsert_document_by_id` ✏️ | Upsert a document by ID |
| `insert_document_by_id` ✏️ | Insert a new document (fails if it exists) |
| `replace_document_by_id` ✏️ | Replace an existing document (fails if it doesn't exist) |
| `delete_document_by_id` ✏️ | Delete a document by ID |
| `mutate_subdocument` ✏️ | Modify paths in an existing document (upsert, insert, replace, remove, array ops, counters) |

#### Query and indexing

| Tool | Description |
| --- | --- |
| `run_sql_plus_plus_query` | Run a [SQL++](https://www.couchbase.com/sqlplusplus/) query scoped to a bucket and scope — use collection names directly (`SELECT * FROM users`). Data-modifying statements are blocked in read-only mode |
| `explain_sql_plus_plus_query` | Generate and evaluate an EXPLAIN plan: metadata, extracted plan and findings |
| `get_index_advisor_recommendations` | Index Advisor recommendations for a SQL++ query |
| `list_indexes` | List indexes with definitions, filterable by bucket/scope/collection/name; flags vector indexes (`is_vector`, `vector_type`) |
| `get_index_stats` 🖥️ | Per-index size, fragmentation, scan traffic and lag, per node — finds pressure and unused indexes |
| `create_query_index` ✏️ | Create a scalar, Hyperscale vector or Composite vector GSI index (`index_type`). Deferred by default — follow with `build_index` |
| `create_index` ✏️ | **Deprecated** — use `create_query_index`. Removed in 2.0 |
| `build_index` ✏️ | Build all deferred indexes on a collection |
| `drop_index` ✏️ | Drop a GSI index (scalar or vector) |

#### Full-text search (FTS)

Requires Couchbase Server 7.6+ and the Search service. Scope-level and cluster-level (legacy) indexes are both supported: pass `bucket_name` and `scope_name` together for scope-level, or omit both.

| Tool | Description |
| --- | --- |
| `list_search_indexes` | List Search indexes — cluster-level, per bucket, or per scope |
| `get_search_index_definition` | Full definition of one Search index (mappings, analyzers, plan params) |
| `run_search` | Run any non-vector FTS query from its raw JSON body; `explain=true` returns the execution plan |
| `upsert_search_index` ✏️ | Create or update a Search index, including vector field mappings. Updates trigger a full rebuild — pass the current `uuid` to avoid clobbering concurrent changes |
| `drop_search_index` ✏️ | Drop a Search index — permanent |

#### Vector search

Both tools embed plain query text with the model configured under [Embedding Providers](#embedding-providers). Available on self-managed Couchbase Server and Capella.

| Tool | Description |
| --- | --- |
| `run_vector_query` | Similarity search against a GSI vector index (Server 8.0+) via `APPROX_VECTOR_DISTANCE()`. The index is chosen automatically; supports a SQL++ `where` prefilter and Hyperscale tuning (`num_probes`, `rerank`, `top_n_scan`). Returns full documents |
| `run_vector_search` | Search-service vector search (Server 7.6+) against a named index. `scalar_query` makes it hybrid; `prefilter` narrows candidates first. Returns index-stored fields per hit |

#### Query performance analysis

| Tool | Description |
| --- | --- |
| `get_longest_running_queries` | Longest-running queries by average service time |
| `get_most_frequent_queries` | Most frequently executed queries |
| `get_queries_with_largest_response_sizes` | Queries with the largest response sizes |
| `get_queries_with_large_result_count` | Queries with the largest result counts |
| `get_queries_using_primary_index` | Queries using a primary index |
| `get_queries_not_using_covering_index` | Queries not using a covering index |
| `get_queries_not_selective` | Queries whose scans — secondary index, primary index or sequential — return far more documents than the final result |

#### Query service health 🖥️

These call the Query service's own admin REST API on every query node.

| Tool | Description |
| --- | --- |
| `get_cluster_query_vitals` | Query-engine health: request rate, active/queued requests, memory, GC, uptime. Needs `ro_admin` |
| `get_active_queries` | Queries executing right now across all query nodes. Needs `ro_admin` |
| `delete_active_query` ✏️ | Cancel an in-flight query by request ID. Needs Full Admin or Cluster Admin |

#### Tools disabled automatically on Capella

The 🖥️ tools reach Couchbase through admin REST endpoints that Capella does not expose. When every host in `CB_CONNECTION_STRING` ends in `.cloud.couchbase.com`, the server does not register them and reports them alongside your own [disabled tools](#disabling-tools) in `get_server_configuration_status` and the startup log. SDK-based tools such as `get_cluster_health_and_services` and `get_client_connectivity_report` stay available.

| Tool | Endpoint it needs |
| --- | --- |
| `get_cluster_metrics` | Management REST (`/pools/default/stats/range`) |
| `get_cluster_tasks` | Management REST (`/pools/default/tasks`) |
| `get_bucket_config` | Management REST (`/pools/default/buckets[/<name>]`) |
| `get_cluster_health_snapshot` | Management REST, plus per-node endpoints |
| `get_cluster_system_events` | Management REST (`/events`) |
| `get_index_stats` | Index Service REST (each indexer's `/api/v1/stats`) |
| `get_cluster_query_vitals` | Query service REST (`/admin/vitals`) |
| `get_active_queries` | Query service REST (`/admin/active_requests`) |
| `delete_active_query` | Query service REST (`DELETE /admin/active_requests/{id}`) |

### Embedding Providers

`run_vector_query` and `run_vector_search` embed query text with the provider set in `CB_MCP_EMBEDDING_MODEL_PROVIDER`. This is optional: without it the server starts normally and both tools stay registered, but calling them returns an error explaining what to set.

| `CB_MCP_EMBEDDING_MODEL_PROVIDER` | Required | Optional | Notes |
| --- | --- | --- | --- |
| `capella` | `CB_MCP_EMBEDDING_MODEL_API_KEY`, `CB_MCP_EMBEDDING_MODEL`, `CB_MCP_EMBEDDING_MODEL_ENDPOINT` | — | Capella Model Service. `CB_MCP_EMBEDDING_MODEL_ENDPOINT` is your deployment's own URL (e.g. `https://<id>.ai.couchbase.com`) from Capella's AI Data Plane > Models |
| `openai` | `CB_MCP_EMBEDDING_MODEL_API_KEY`, `CB_MCP_EMBEDDING_MODEL` | `CB_MCP_EMBEDDING_MODEL_ENDPOINT` | Set the endpoint to use an OpenAI-compatible local server (Ollama, vLLM, LM Studio) |
| `cohere` | `CB_MCP_EMBEDDING_MODEL_API_KEY`, `CB_MCP_EMBEDDING_MODEL` | `CB_MCP_EMBEDDING_MODEL_ENDPOINT` | |
| `voyage` | `CB_MCP_EMBEDDING_MODEL_API_KEY`, `CB_MCP_EMBEDDING_MODEL` | `CB_MCP_EMBEDDING_MODEL_ENDPOINT` | |
| `bedrock` | `CB_MCP_EMBEDDING_MODEL` | `CB_MCP_EMBEDDING_MODEL_AWS_ACCESS_KEY_ID`, `CB_MCP_EMBEDDING_MODEL_AWS_SECRET_ACCESS_KEY`, `CB_MCP_EMBEDDING_MODEL_AWS_REGION` | Amazon Titan embedding models (e.g. `amazon.titan-embed-text-v2:0`) — the provider sends Titan's `inputText` request shape, so other Bedrock embedding models such as Cohere Embed are not supported yet. Omit credentials to use the default AWS chain. Needs the extra: `pip install couchbase-mcp-server[bedrock-embeddings]` |

```json
"env": {
  "CB_CONNECTION_STRING": "couchbases://connection-string",
  "CB_USERNAME": "username",
  "CB_PASSWORD": "password",
  "CB_MCP_EMBEDDING_MODEL_PROVIDER": "openai",
  "CB_MCP_EMBEDDING_MODEL": "text-embedding-3-small",
  "CB_MCP_EMBEDDING_MODEL_API_KEY": "sk-..."
}
```

### All Options

<details>
<summary>Every environment variable and CLI flag for the <code>operational</code> server</summary>

The [Operational Insights server](#operational-insights-server) accepts the same options, except that it has its own [connection settings](#operational-insights-connection-and-authentication) and no `CB_MCP_EMBEDDING_MODEL_*` options.

| Environment Variable | CLI Argument | Description | Default |
| ---------------------------- | ------------------------ | ------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `CB_CONNECTION_STRING` | `--connection-string` | Connection string to the Couchbase cluster | **Required** |
| `CB_USERNAME` | `--username` | Username with access to required buckets for basic authentication | **Required (or Client Certificate and Key needed for mTLS)** |
| `CB_PASSWORD` | `--password` | Password for basic authentication | **Required (or Client Certificate and Key needed for mTLS)** |
| `CB_CLIENT_CERT_PATH` | `--client-cert-path` | Path to the client certificate file for mTLS authentication | **Required if using mTLS (or Username and Password required)** |
| `CB_CLIENT_KEY_PATH` | `--client-key-path` | Path to the client key file for mTLS authentication | **Required if using mTLS (or Username and Password required)** |
| `CB_CA_CERT_PATH` | `--ca-cert-path` | Path to server root certificate for TLS if server is configured with a self-signed/untrusted certificate. This will not be required if you are connecting to Capella | |
| `CB_MCP_READ_ONLY_MODE` | `--read-only-mode` | Prevent all data modifications (KV, Query, scope/collection management, index management, and FTS index management). When enabled, write tools are not loaded. | `true` |
| `CB_MCP_TRANSPORT` | `--transport` | Transport mode: `stdio`, `http`, `sse` | `stdio` |
| `CB_MCP_HOST` | `--host` | Host for HTTP/SSE transport modes | `127.0.0.1` |
| `CB_MCP_PORT` | `--port` | Port for HTTP/SSE transport modes | `8000` |
| `CB_MCP_WORKERS` | `--workers` | Number of server worker processes for the `http` transport. Values above 1 run in stateless HTTP mode (see [Multiple Workers](#multiple-workers)) | `1` |
| `CB_MCP_STATELESS_HTTP` | `--stateless-http` | Handle each HTTP request without per-session state. Only honored with `http`; cannot be combined with confirmation-required tools (see [Multiple Workers](#multiple-workers)) | `true` when workers > 1, otherwise `false` |
| `CB_MCP_DISABLED_TOOLS` | `--disabled-tools` | Tools to disable (see [Disabling Tools](#disabling-tools)) | None |
| `CB_MCP_CONFIRMATION_REQUIRED_TOOLS` | `--confirmation-required-tools` | Tools that require explicit user confirmation before execution via MCP elicitation (see [Requiring confirmation](#requiring-confirmation-elicitation)) | None |
| `CB_MCP_LOG_LEVEL` | `--log-level` | Logging level for the MCP server: `off`, `debug`, `info`, `warning`, `error` (see [Logging](#logging)) | `info` |
| `CB_MCP_LOG_SINKS` | `--log-sinks` | Comma-separated log destinations: `stderr`, `file`, or both (see [Logging](#logging)) | `stderr` |
| `CB_MCP_LOG_FILE` | `--log-file` | Base path for per-level log files (only used when the `file` sink is enabled) | `mcp_server.log` |
| `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` | `--log-rotation-max-size-mb` | Global maximum size **in MB** per log file before it rotates, inherited by every level unless overridden. `0` — or a value that cannot be used, such as a negative, `nan` or `inf` — is invalid and falls back to the default with a startup warning | `1` (1 MB) |
| `CB_MCP_LOG_MAX_BYTES` | `--log-max-bytes` | **Deprecated** — use `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` (MB). Global rotation size **in bytes**, still honored for backward compatibility; ignored when `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` is also set | Unset |
| `CB_MCP_LOG_ERROR_ROTATION_MAX_SIZE_MB` | `--log-error-rotation-max-size-mb` | Rotation size **in MB** for the ERROR log file; overrides `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` for ERROR | Inherits `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` |
| `CB_MCP_LOG_WARNING_ROTATION_MAX_SIZE_MB` | `--log-warning-rotation-max-size-mb` | Rotation size **in MB** for the WARNING log file; overrides `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` for WARNING | Inherits `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` |
| `CB_MCP_LOG_INFO_ROTATION_MAX_SIZE_MB` | `--log-info-rotation-max-size-mb` | Rotation size **in MB** for the INFO log file; overrides `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` for INFO | Inherits `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` |
| `CB_MCP_LOG_DEBUG_ROTATION_MAX_SIZE_MB` | `--log-debug-rotation-max-size-mb` | Rotation size **in MB** for the DEBUG log file; overrides `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` for DEBUG | Inherits `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` |
| `CB_MCP_LOG_RETENTION_BACKUP_COUNT` | `--log-retention-backup-count` | Rotated backup files kept per-level log file (excluding the live file), applied to every level unless overridden. `0` keeps only the live file (see [Logging](#logging)) | `1` |
| `CB_MCP_LOG_ERROR_RETENTION_BACKUP_COUNT` | `--log-error-retention-backup-count` | Rotated backups kept for the ERROR log file; overrides the global count for ERROR | Inherits `CB_MCP_LOG_RETENTION_BACKUP_COUNT` |
| `CB_MCP_LOG_WARNING_RETENTION_BACKUP_COUNT` | `--log-warning-retention-backup-count` | Rotated backups kept for the WARNING log file; overrides the global count for WARNING | Inherits `CB_MCP_LOG_RETENTION_BACKUP_COUNT` |
| `CB_MCP_LOG_INFO_RETENTION_BACKUP_COUNT` | `--log-info-retention-backup-count` | Rotated backups kept for the INFO log file; overrides the global count for INFO | Inherits `CB_MCP_LOG_RETENTION_BACKUP_COUNT` |
| `CB_MCP_LOG_DEBUG_RETENTION_BACKUP_COUNT` | `--log-debug-retention-backup-count` | Rotated backups kept for the DEBUG log file; overrides the global count for DEBUG | Inherits `CB_MCP_LOG_RETENTION_BACKUP_COUNT` |
| `CB_MCP_OAUTH_JWT_JWKS_URI` | `--oauth-jwks-uri` | JWKS endpoint of the identity provider used to verify bearer JWTs. Enables OAuth when set with the issuer and audience (see [OAuth 2.1 Authorization](#oauth-21-authorization)) | None |
| `CB_MCP_OAUTH_JWT_ISSUER` | `--oauth-issuer` | Expected JWT `iss` claim. Required to enable OAuth | None |
| `CB_MCP_OAUTH_JWT_AUDIENCE` | `--oauth-audience` | Expected JWT `aud` claim. Required to enable OAuth | None |
| `CB_MCP_OAUTH_JWT_ALGORITHM` | `--oauth-algorithm` | JWT signing algorithm: one of `RS256/384/512`, `ES256/384/512`, `PS256/384/512` | `RS256` |
| `CB_MCP_OAUTH_MCP_BASE_URL` | `--oauth-mcp-base-url` | Public base URL of this server. When set, publishes RFC 9728 Protected Resource Metadata so PRM-aware clients can discover the IdP | None |
| `CB_MCP_OAUTH_SCOPE_READ_LABEL` | `--oauth-scope-read-label` | Override the OAuth scope label treated as 'read' access (advertised in PRM and matched against the token's `scope`/`scp` claim). Use when your IdP can't emit the canonical form | `couchbase-mcp:read` |
| `CB_MCP_OAUTH_SCOPE_WRITE_LABEL` | `--oauth-scope-write-label` | Override the OAuth scope label treated as 'write' access; same semantics as the read label | `couchbase-mcp:write` |
| `CB_MCP_AUDIT_LOG_ENABLED` | `--audit-log-enabled` | Enable audit logging — a separate sink from the `CB_MCP_LOG_*` operational logs, with a stable schema and its own retention (see [Auditing](#auditing)) | `false` |
| `CB_MCP_AUDIT_LOG_SINKS` | `--audit-log-sinks` | Comma-separated audit destinations: `console`, `file`, or both. `console` writes to **stderr** — never stdout, which carries the JSON-RPC protocol on stdio | `console` |
| `CB_MCP_AUDIT_LOG_FILE_PATH` | `--audit-log-file-path` | Path to the audit log file. Required when `file` is in the sinks; the hostname and process id are inserted before the extension (`audit.log` → `audit.<host>.<pid>.log`) | None |
| `CB_MCP_AUDIT_LOG_ROTATION_MAX_SIZE_MB` | `--audit-log-rotation-max-size-mb` | Maximum size **in MB** the live audit file may reach before it rotates. `0` turns size-based rotation **off** | `10` (10 MB) |
| `CB_MCP_AUDIT_LOG_ROTATION_INTERVAL` | `--audit-log-rotation-interval` | Age the live audit file may reach before it rotates, as `<value><unit>` where the unit is `d` (24 hours) or `w` (7 days) — e.g. `1d`, `30d`, `8w`. `0` turns interval-based rotation **off** | `1d` |
| `CB_MCP_AUDIT_LOG_RETENTION_MAX_BACKUPS` | `--audit-log-retention-max-backups` | Rotated backups retained **per audit file**, excluding the live file. Backups are gzipped. `0` keeps none. This bounds one file, not the directory — see [Auditing](#auditing) | `10` |
| `CB_MCP_AUDIT_LOG_TOOL_ARGS` | `--audit-log-tool-args` | Record tool argument values in the audit log. **Off by default** — there is no redaction in this release, so enabling it writes arguments verbatim, including full document bodies | `false` |
| `CB_MCP_AUDIT_LOG_DISABLED_EVENTS` | `--audit-log-disabled-events` | Audit events to suppress: comma-separated numeric event ids (e.g. `61490,61491`), or a file with one id per line. Only filterable events can be suppressed | None |
| `CB_MCP_EMBEDDING_MODEL_PROVIDER` | `--embedding-model-provider` | Embedding provider for `run_vector_query` / `run_vector_search`: one of `capella`, `openai`, `cohere`, `voyage`, `bedrock`. Unset disables both tools' embedding step until configured — see [Embedding Provider Configuration](#embedding-providers) | None |
| `CB_MCP_EMBEDDING_MODEL` | `--embedding-model` | Model name/ID for the configured embedding provider | None |
| `CB_MCP_EMBEDDING_MODEL_API_KEY` | `--embedding-model-api-key` | API key for the configured provider. Not used by `bedrock` (uses the AWS credential chain / `CB_MCP_EMBEDDING_MODEL_AWS_*` instead) | None |
| `CB_MCP_EMBEDDING_MODEL_ENDPOINT` | `--embedding-model-endpoint` | Base URL override (an OpenAI-compatible local server, or a Capella Model Service deployment's own URL — **required** when `CB_MCP_EMBEDDING_MODEL_PROVIDER=capella`) | None |
| `CB_MCP_EMBEDDING_MODEL_AWS_ACCESS_KEY_ID` | `--embedding-model-aws-access-key-id` | AWS access key ID, `bedrock` provider only. Omit to use the default AWS credential chain | None |
| `CB_MCP_EMBEDDING_MODEL_AWS_SECRET_ACCESS_KEY` | `--embedding-model-aws-secret-access-key` | AWS secret access key, `bedrock` provider only | None |
| `CB_MCP_EMBEDDING_MODEL_AWS_REGION` | `--embedding-model-aws-region` | AWS region, `bedrock` provider only. Falls back to the AWS SDK's own region resolution if unset | None |

</details>

## Operational Insights Server

The `operational-insights` server connects to self-managed [Operational Insights](https://docs.couchbase.com/operational-insights/current/intro/intro.html) clusters. Operational Insights is a real-time analytical database (RT-OLAP) for real-time apps and operational intelligence. The server is **not available for Capella**. It uses the [`couchbase-operational-insights`](https://docs.couchbase.com/python-operational-insights-sdk/current/hello-world/overview.html) SDK. It runs as its own process, separate from the operational server.

> [!IMPORTANT]
> `CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING` is an **HTTP(S) URL**, not a `couchbase://` connection string — e.g. `http://localhost:8095`, or `https://<host>:18095` over TLS. This is the most common misconfiguration.

### Operational Insights Quick Start

**From PyPI with uvx:**

```json
{
  "mcpServers": {
    "couchbase-operational-insights": {
      "command": "uvx",
      "args": ["couchbase-mcp-server", "operational-insights"],
      "env": {
        "CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING": "http://localhost:8095",
        "CB_OPERATIONAL_INSIGHTS_USERNAME": "Administrator",
        "CB_OPERATIONAL_INSIGHTS_PASSWORD": "password"
      }
    }
  }
}
```

**With Docker:**

```json
{
  "mcpServers": {
    "couchbase-operational-insights": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING=http://host.docker.internal:8095",
        "-e", "CB_OPERATIONAL_INSIGHTS_USERNAME=Administrator",
        "-e", "CB_OPERATIONAL_INSIGHTS_PASSWORD=password",
        "couchbase/mcp-server:latest", "operational-insights"
      ]
    }
  }
}
```

From source, use the [operational source configuration](#operational-quick-start) with `operational-insights` as the last argument and the variables above.

### Operational Insights Connection and Authentication

| Environment Variable | CLI Argument | Description |
| --- | --- | --- |
| `CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING` | `--connection-string` | Endpoint URL (HTTP/HTTPS). **Required** |
| `CB_OPERATIONAL_INSIGHTS_USERNAME` | `--username` | Username |
| `CB_OPERATIONAL_INSIGHTS_PASSWORD` | `--password` | Password |
| `CB_OPERATIONAL_INSIGHTS_CA_CERT_PATH` | `--ca-cert-path` | Root certificate (PEM) for a self-signed/untrusted server certificate |
| `CB_OPERATIONAL_INSIGHTS_CLIENT_CERT_PATH` | `--client-cert-path` | Client certificate for mTLS — PEM (with a key) or a PKCS#12 bundle (`.p12`/`.pfx`, no key). Requires an `https://` URL; overrides username/password |
| `CB_OPERATIONAL_INSIGHTS_CLIENT_KEY_PATH` | `--client-key-path` | Private key (PEM) for the client certificate. Leave unset for PKCS#12 |
| `CB_OPERATIONAL_INSIGHTS_CLIENT_CERT_PASSWORD` | `--client-cert-password` | Password for an encrypted key or PKCS#12 bundle |
| `CB_MCP_OPERATIONAL_INSIGHTS_QUERY_RESULT_MAX_SIZE` | `--operational-insights-query-result-max-size` | Maximum query result size **in bytes**. Unset means no limit. When set, reading stops at the budget and the response reports `truncated: true`; a row that would overrun it is not returned. Values above 4 MB are clamped |

All [common configuration](#common-configuration) applies, with these differences from the operational server:

- The default port is `8001` and the default log file is `mcp_server_operational_insights.log`, so both servers can run side by side.
- Only one worker is supported (`CB_MCP_WORKERS=1`), because async query handles live in process memory.
- It has no `CB_MCP_EMBEDDING_MODEL_*` options.

### Operational Insights Tools

All names except `get_server_configuration_status` are prefixed `operational_insights_`, so both servers can be registered in one client without collisions. The write tool (marked ✏️) is **not loaded** while [read-only mode](#read-only-mode) is on, the default.

| Tool | Description |
| --- | --- |
| `get_server_configuration_status` | Same tool as on the operational server |
| `operational_insights_get_databases_in_cluster` | List databases |
| `operational_insights_get_scopes_in_database` | List scopes in a database |
| `operational_insights_get_collections_in_scope` | List collections (datasets) in a scope |
| `operational_insights_get_schema_for_collection` | Infer a collection's JSON schema by sampling |
| `operational_insights_list_indexes` | List secondary indexes from `System.Metadata.Index` |
| `operational_insights_explain_query` | EXPLAIN a SQL++ statement without running it |
| `operational_insights_run_query_sync` | Run SQL++ and return rows, up to the result size limit; can [export](#exporting-large-results) instead. Read-only mode is enforced server-side |
| `operational_insights_run_query_async` | Start a query and return a `query_handle`; preferred for large exports |
| `operational_insights_get_async_query_results` | Poll an async query; returns rows (or the export destination) when ready |
| `operational_insights_discard_async_query_results` | Free a finished async query's results |
| `operational_insights_cancel_async_query` | Stop a still-running async query (allowed in read-only mode) |
| `operational_insights_create_index` ✏️ | Create a secondary index via `CREATE INDEX` |

**Async queries** follow a start → poll → discard-or-cancel flow: `operational_insights_run_query_async` returns a `query_handle`, `operational_insights_get_async_query_results` is polled until ready, then `operational_insights_discard_async_query_results` frees the results, or `operational_insights_cancel_async_query` stops a query still running.

### Exporting large results

Instead of returning rows, `operational_insights_run_query_sync` and `operational_insights_run_query_async` can wrap the statement in `COPY ... TO` and write the rows to external object storage. Pass all three of `copy_to_link` (an existing link created with `CREATE LINK ... TYPE S3`), `copy_to_bucket` and `copy_to_path`, plus optionally `copy_to_format` (`json`, the default, or `parquet`). The tool then returns a confirmation with no rows:

```json
{
  "success": true,
  "exported": true,
  "destination": {"link": "s3Link", "bucket": "my-exports", "path": "exports/run1", "format": "json"},
  "message": "Results were written to my-exports/exports/run1 as json. ..."
}
```

- A partial destination is rejected rather than silently returning rows.
- The sync tool waits for the whole upload; prefer the async tool for large exports.
- Exports are **blocked in read-only mode** (they write to external storage), so they need `CB_MCP_READ_ONLY_MODE=false` or a token with `couchbase-mcp:write`.
- `csv` is not offered, because it needs an explicit output schema that can't be inferred from an arbitrary `SELECT`.

## Common Configuration

These settings work the same way for both servers. Every option can be set as an environment variable or as a CLI flag after the subcommand.

### Safety Controls

Three layers, from broadest to narrowest. Use them together — but remember that **none of them replaces RBAC**.

> [!IMPORTANT]
> The database user's RBAC permissions are the authoritative security control. For example, disabling `upsert_document_by_id` and `delete_document_by_id` does not stop `run_sql_plus_plus_query` from running `INSERT`/`UPDATE`/`DELETE`/`MERGE` — only read-only mode or missing RBAC permissions do. Give the server's database user only the permissions it needs.

#### Read-only mode

`CB_MCP_READ_ONLY_MODE` (default `true`) is the single switch for writes:

- `true` — the write tools marked ✏️ in each server's tool list are **not loaded**, and queries that modify data or structure are blocked.
- `false` — write tools are loaded and modifying queries are allowed.

#### Disabling tools

`CB_MCP_DISABLED_TOOLS` removes tools from discovery entirely. It accepts a comma-separated list or a path to a file with one tool name per line (`#` starts a comment):

```bash
CB_MCP_DISABLED_TOOLS="upsert_document_by_id,delete_document_by_id"
# or
CB_MCP_DISABLED_TOOLS=/path/to/disabled_tools.txt
```

```text
# disabled_tools.txt
# Write operations
upsert_document_by_id
delete_document_by_id

# Index advisor
get_index_advisor_recommendations
```

On Capella, the operational server also [disables some admin tools automatically](#tools-disabled-automatically-on-capella).

#### Requiring confirmation (elicitation)

`CB_MCP_CONFIRMATION_REQUIRED_TOOLS` asks the user to confirm before a listed tool runs, using MCP [elicitation](https://modelcontextprotocol.io/specification/2025-06-18/server/elicitation). It takes the same comma-separated or file formats as disabled tools:

```bash
CB_MCP_CONFIRMATION_REQUIRED_TOOLS="delete_document_by_id,replace_document_by_id"
```

- If the client supports elicitation, the user is prompted before the tool runs.
- If it doesn't, the tool runs without confirmation, for backward compatibility.
- Confirmation needs an MCP session, so it cannot be combined with stateless HTTP (`CB_MCP_STATELESS_HTTP=true` or `CB_MCP_WORKERS` above 1). The server refuses to start with that combination.

### Transports

| Transport | Use for | Endpoint |
| --- | --- | --- |
| `stdio` (default) | One local client that launches the server as a subprocess | — |
| `http` | Several clients sharing one server; supports OAuth and health checks, and workers on the operational server | `http://<host>:<port>/mcp` |
| `sse` | **Deprecated** by the MCP spec — use `http` | `http://<host>:<port>/sse` |

Set the transport with `CB_MCP_TRANSPORT`, the bind address with `CB_MCP_HOST` (default `127.0.0.1`) and the port with `CB_MCP_PORT` (each server's default port when unset). Without [OAuth](#oauth-21-authorization), the HTTP endpoint is unauthenticated.

```bash
uvx couchbase-mcp-server operational \
  --connection-string='<couchbase_connection_string>' \
  --username='<database_username>' \
  --password='<database_password>' \
  --transport=http
```

Then point your client at the URL:

```json
{
  "mcpServers": {
    "couchbase-http": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

#### Multiple workers

One process uses at most about one CPU core. `--workers N` (`CB_MCP_WORKERS`) runs N processes behind the same host and port; the number of available cores is a good starting point. Workers are supported by the operational server only. With more than one worker:

- The `http` transport is required, and the server runs in stateless HTTP mode, because a client's requests can reach different workers. You can also set `--stateless-http=true` on a single worker, for example behind a load balancer without sticky sessions.
- [Confirmation-required tools](#requiring-confirmation-elicitation) cannot be used.
- Each process writes its own log files, named with host and process id (`mcp_server.<host>.<pid>.info.log`) — one set for the supervisor and one per worker.

#### Health check

In HTTP mode, `GET /health` returns `{"status": "ok", "server": "<server>"}` on the MCP port. It is a liveness check: it does not connect to the cluster (so a cluster outage doesn't restart healthy containers), needs no authentication even with OAuth enabled, and is served by every worker. See [Docker](#docker) for probe examples.

### OAuth 2.1 Authorization

With `--transport=http`, either server can act as an **OAuth 2.1 resource server**, validating bearer JWTs against your identity provider's JWKS. It works with any OAuth 2.1/OIDC provider that publishes a JWKS, and does not issue tokens or manage users. OAuth settings are ignored on `stdio`.

- OAuth turns on only when all three of `CB_MCP_OAUTH_JWT_JWKS_URI`, `CB_MCP_OAUTH_JWT_ISSUER` and `CB_MCP_OAUTH_JWT_AUDIENCE` are set; setting only some fails at startup.
- `CB_MCP_OAUTH_MCP_BASE_URL` additionally publishes RFC 9728 Protected Resource Metadata so clients can discover the authorization server.
- Access is gated by two scopes in the token's `scope`/`scp` claim: `couchbase-mcp:read` (read tools) and `couchbase-mcp:write` (write tools). Full access needs both. Rename them with `CB_MCP_OAUTH_SCOPE_READ_LABEL` / `CB_MCP_OAUTH_SCOPE_WRITE_LABEL` if your IdP can't emit these labels. Both servers use the same labels, so one IdP configuration covers both.

```bash
uvx couchbase-mcp-server operational \
  --connection-string='<couchbase_connection_string>' \
  --username='<database_username>' \
  --password='<database_password>' \
  --transport=http \
  --oauth-jwks-uri='https://auth.example.com/.well-known/jwks.json' \
  --oauth-issuer='https://auth.example.com/' \
  --oauth-audience='couchbase-mcp-server' \
  --oauth-mcp-base-url='<public_base_url_of_this_server>'
```

See the [OAuth documentation](https://docs.couchbase.com/mcp-server/configuration/oauth-overview.html) for IdP setup.

### Logging

Each server logs to `stderr` at `info` level by default.

- **`CB_MCP_LOG_LEVEL`** — `off`, `error`, `warning`, `info` (lifecycle events and tool calls) or `debug`.
- **`CB_MCP_LOG_SINKS`** — `stderr`, `file`, or both. With `file`, one rotating file per level is written next to `CB_MCP_LOG_FILE` (`mcp_server.info.log`, `mcp_server.error.log`, …).
- **Rotation and retention** — `CB_MCP_LOG_ROTATION_MAX_SIZE_MB` (default 1 MB) and `CB_MCP_LOG_RETENTION_BACKUP_COUNT` (default 1) apply to every level; override per level with `CB_MCP_LOG_<LEVEL>_ROTATION_MAX_SIZE_MB` / `CB_MCP_LOG_<LEVEL>_RETENTION_BACKUP_COUNT`. Invalid sizes fall back to the default with a startup warning. `CB_MCP_LOG_MAX_BYTES` is deprecated.
- **Config snapshot** — with the `file` sink, a JSON record of the environment and redacted server config is written to `mcp_server_config.log.json` on every start, for support.

```bash
# Debug logging to stderr and rotating per-level files
uvx couchbase-mcp-server operational --log-level=debug --log-sinks=stderr,file
```

Your MCP client usually also captures the server's stderr — see [Client Setup](#client-setup) for where. Details: [logging documentation](https://docs.couchbase.com/mcp-server/configuration/logging.html).

### Auditing

Audit logging records **who did what, and whether it was allowed** — separate from the operational logs, which record what went wrong. The MCP server is the only layer that can answer this for an agent: every request reaches the cluster as a single database user, and decisions such as a scope denial, a read-only block or a declined confirmation never reach the cluster at all.

Auditing is **off by default**. Turn it on with `CB_MCP_AUDIT_LOG_ENABLED=true`; it writes to stderr unless you set `CB_MCP_AUDIT_LOG_SINKS=file` and `CB_MCP_AUDIT_LOG_FILE_PATH`.

```bash
# 90 days of daily, gzipped audit files
uvx couchbase-mcp-server operational --audit-log-enabled=true \
  --audit-log-sinks=file --audit-log-file-path=/var/log/cb-mcp/audit.log \
  --audit-log-rotation-interval=1d --audit-log-rotation-max-size-mb=0 \
  --audit-log-retention-max-backups=90
```

Records are JSON Lines using the same field vocabulary as Couchbase Server and Sync Gateway:

```json
{"id":61522,"name":"document write","description":"A document write operation was requested through a tool","timestamp":"2026-09-29T09:14:22.418Z","real_userid":{"domain":"oauth","user":"agent-svc-7"},"server":{"version":"1.0.2","host":"mcp-01"},"cid":"9f2c...","outcome":"success","tool_name":"upsert_document_by_id","required_scope":"write","ks":"travel-sample.inventory.airline","cb_userid":"mcp_service"}
```

<details>
<summary>How auditing behaves</summary>

- **Identity** — `real_userid` is the caller: `oauth` (the token subject), `local` (the OS user, on stdio) or `anonymous` (HTTP without OAuth, which warns at startup).
- **Correlation** — `cid` groups every record from one request, and is sent as the SQL++ `client_context_id`, so it joins exactly with Couchbase Server's `clientContextId`. There is deliberately no session identifier.
- **Events** — a fixed, numbered catalogue (lifecycle, initialize, token rejected, scope denied, read-only block, confirmation declined/skipped, and a read/write pair per tool category). The full catalogue ships as `descriptor.json` in the package. The operational server uses block `0xF000` (61440), Operational Insights `0x10000` (65536), and both share `0xE000` (57344) for core events.
- **Filtering** — `CB_MCP_AUDIT_LOG_DISABLED_EVENTS` suppresses *read* events by numeric id (e.g. `61490,61491`). Write and security events cannot be disabled.
- **Rotation** — by size (`CB_MCP_AUDIT_LOG_ROTATION_MAX_SIZE_MB`, default 10) and age (`CB_MCP_AUDIT_LOG_ROTATION_INTERVAL`, default `1d`), whichever comes first; `0` switches either off. Rotated files are timestamped and gzipped; the live file stays plain text for `tail -f`.
- **Per-process files** — host and pid are inserted into the name (`audit.log` → `audit.<host>.<pid>.log`), so concurrent processes never share a file.
- **Retention is per file** — `CB_MCP_AUDIT_LOG_RETENTION_MAX_BACKUPS` (default 10) bounds one file's backups. Each new process writes a new file and nothing prunes old ones, so size the volume or sweep old `audit.*.log*` files.
- **Fail-open** — if the file can't be written, the server keeps serving and counts dropped records; `get_server_configuration_status` reports `written` / `dropped` / `write_errors`.
- **Tool arguments** — recorded only with `CB_MCP_AUDIT_LOG_TOOL_ARGS=true`, which warns at startup. There is no redaction in this release, so arguments, including full document bodies, are written verbatim.

</details>

## Client Setup

Each client needs a server configuration from the [operational](#operational-quick-start) or [Operational Insights](#operational-insights-quick-start) quick start in its own configuration location. The examples below use the operational server.

<details>
<summary>Claude Desktop</summary>

1. Open the configuration file (see the [MCP quickstart guide](https://modelcontextprotocol.io/quickstart/user)):
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. Add the server under `mcpServers` and restart Claude Desktop.

**Logs:** macOS `~/Library/Logs/Claude`, Windows `%APPDATA%\Claude\Logs`. See the [MCP troubleshooting guide](https://modelcontextprotocol.io/quickstart/user#troubleshooting).

</details>

<details>
<summary>Cursor</summary>

1. Go to **Cursor Settings > Tools & Integrations > MCP Tools** (see [Cursor's MCP docs](https://docs.cursor.com/en/context/mcp#configuring-mcp-servers)), or use the one-click [Install in Cursor][cursor-install-basic] link — then replace the placeholder connection string and credentials.
2. Add the configuration under `mcpServers` and save.
3. Refresh the MCP servers list to check that `couchbase` is enabled.

**Logs:** in the bottom panel, open **Output** and select **Cursor MCP**.

[cursor-install-basic]: https://cursor.com/en-US/install-mcp?name=Couchbase&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJjb3VjaGJhc2UtbWNwLXNlcnZlciJdLCJlbnYiOnsiQ0JfQ09OTkVDVElPTl9TVFJJTkciOiJjb3VjaGJhc2VzOi8vY29ubmVjdGlvbi1zdHJpbmciLCJDQl9VU0VSTkFNRSI6InVzZXJuYW1lIiwiQ0JfUEFTU1dPUkQiOiJwYXNzd29yZCJ9fQ%3D%3D

</details>

<details>
<summary>Windsurf Editor</summary>

1. Open **Command Palette > Windsurf MCP Configuration Panel**, or **Windsurf Settings > Advanced > Cascade > Model Context Protocol (MCP) Servers** (see [Windsurf's MCP docs](https://docs.windsurf.com/windsurf/cascade/mcp)).
2. Click **Add Server > Add custom server**, paste the configuration and save.
3. Refresh the server list to check that `couchbase` is enabled.

</details>

<details>
<summary>VS Code</summary>

VS Code uses `servers` as the top-level key instead of `mcpServers`. Put the configuration in `.vscode/mcp.json` for a workspace, or run **MCP: Open User Configuration** for all workspaces (see [VS Code's MCP docs](https://code.visualstudio.com/docs/copilot/customization/mcp-servers)):

```json
{
  "servers": {
    "couchbase": {
      "command": "uvx",
      "args": ["couchbase-mcp-server", "operational"],
      "env": {
        "CB_CONNECTION_STRING": "couchbases://connection-string",
        "CB_USERNAME": "username",
        "CB_PASSWORD": "password"
      }
    }
  }
}
```

Saving the file starts the server; use the `Running | Stop | n Tools` actions above it to manage it.

**Logs:** run **MCP: List Servers**, pick `couchbase`, then **Show Output**.

</details>

<details>
<summary>JetBrains IDEs</summary>

1. Install the [AI Assistant](https://www.jetbrains.com/help/ai-assistant/getting-started-with-ai-assistant.html) or [Junie](https://www.jetbrains.com/help/junie/get-started-with-junie.html) plugin.
2. Go to **Settings > Tools > AI Assistant (or Junie) > MCP Server**, click **+**, add the configuration and click **Apply**.
3. Hover over the server's status to see its tools.

**Logs:** **Help > Show Log in Finder (Explorer) > mcp > couchbase**.

</details>

## Docker

Prebuilt images are published to [Docker Hub](https://hub.docker.com/r/couchbase/mcp-server) as `couchbase/mcp-server`, and the server is listed in the [Docker MCP Catalog](https://hub.docker.com/mcp/server/couchbase/overview). Configure the container with the same environment variables as above. See the [Docker Hub page](https://hub.docker.com/r/couchbase/mcp-server) ([DOCKER.md](DOCKER.md)) for tags, volumes and health-check examples.

Running over HTTP — note that the server must bind to `0.0.0.0` to be reachable from outside the container:

```bash
docker run --rm \
  -e CB_CONNECTION_STRING='<couchbase_connection_string>' \
  -e CB_USERNAME='<database_user>' \
  -e CB_PASSWORD='<database_password>' \
  -e CB_MCP_TRANSPORT=http \
  -e CB_MCP_HOST=0.0.0.0 \
  -p 8000:8000 \
  couchbase/mcp-server:latest operational
```

- If Couchbase runs on your host machine, use `couchbase://host.docker.internal` as the connection string (see [Docker networking](https://docs.docker.com/desktop/features/networking/#i-want-to-connect-from-a-container-to-a-service-on-the-host)). Use `--network=<your_network>` to join another container's network.
- Append `operational-insights` instead of `operational` to run the [Operational Insights server](#operational-insights-server) (port `8001`).

<details>
<summary>Building the image from source</summary>

```bash
docker build -t mcp/couchbase-src .
```

`./build.sh [image-name]` builds with the same arguments as CI — it sets the `GIT_COMMIT_HASH` and `BUILD_DATE` build args and tags the image `latest` and `<short-commit>` (default name `mcp/couchbase-src`). To build with the labels by hand:

```bash
docker build --build-arg GIT_COMMIT_HASH=$(git rev-parse HEAD) \
  --build-arg BUILD_DATE=$(date -u +'%Y-%m-%dT%H:%M:%SZ') \
  -t mcp/couchbase-src .

# Check the labels
docker inspect --format='{{json .Config.Labels}}' mcp/couchbase-src:latest
```

</details>

## Troubleshooting

- Check the connection string, username/password or certificate paths.
- For Capella, make sure the cluster [allows connections](https://docs.couchbase.com/cloud/clusters/allow-ip-address.html) from the machine running the server.
- Make sure the database user can access at least one bucket.
- If the client can't find `uv`/`uvx`, put the absolute path in `command`.
- When running from source, check the repository path in the configuration, and run `uv sync` after pulling updates.
- Call `get_server_configuration_status` to see the configuration the server actually resolved, and check your client's logs (see [Client Setup](#client-setup)).

## FAQ

**How do I connect Claude Desktop to Couchbase?** Add the [quick start](#operational-quick-start) configuration to `claude_desktop_config.json` and restart Claude Desktop — see [Client Setup](#client-setup).

**Can I use this with Couchbase Capella?** Yes, with the [operational server](#operational-server): the same configuration works for Capella and self-managed clusters, though a few admin tools are only available on self-managed clusters (see [Tools](#operational-tools)). The [Operational Insights server](#operational-insights-server) is for self-managed Operational Insights clusters only.

**Is it safe to let an AI agent write to my database?** By default, no write tools are loaded and modifying SQL++ is blocked. If you enable writes, you can still disable individual tools or require confirmation — but your Couchbase user's RBAC permissions remain the real security boundary. See [Safety Controls](#safety-controls).

**Do I need to know SQL++?** No. Ask in plain English and the assistant writes the SQL++ for `run_sql_plus_plus_query`. It can also explain a query plan or ask the Index Advisor for recommendations.

**STDIO, Streamable HTTP or SSE?** STDIO for a single local client. Streamable HTTP to share one server between clients, with OAuth. SSE is deprecated. See [Transports](#transports).

**Is this officially supported by Couchbase?** Yes, with a [Couchbase AI Data Plane](https://www.couchbase.com/downloads/?family=ai-data-plane) license. Without one, Couchbase engineers help on a best-effort basis through GitHub issues. See [Support Policy](#usage-data-llm-risks-and-support-policy).

## Contributing

Contributions are welcome — bug fixes, features and documentation. To report a bug or ask for help, [open a GitHub issue](https://github.com/couchbase/mcp-server-couchbase/issues).

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, linting, testing, project structure and workflow. To get started:

```bash
git clone https://github.com/couchbase/mcp-server-couchbase.git
cd mcp-server-couchbase
uv sync --extra dev
uv run pre-commit install
./scripts/lint.sh
```

**Integration tests** run against a live cluster. Export `CB_CONNECTION_STRING`, `CB_USERNAME`, `CB_PASSWORD` and optionally `CB_MCP_TEST_BUCKET`; the Operational Insights tests also use `CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING` / `_USERNAME` / `_PASSWORD` and are skipped when those are unset. Then run:

```bash
uv run --extra dev pytest tests/integration -v
```

## Usage Data, LLM Risks and Support Policy

### Usage Data Collection

This product automatically collects usage and performance data (such as product name and version) and browser information (such as IP address) (collectively, "Usage Data"). Couchbase uses Usage Data, along with other data you may provide to Couchbase (such as your user name or email address), to develop and improve our products as well as inform our sales and marketing programs. We do not access or collect any data you store in Couchbase products. We use Usage Data to understand aggregate usage patterns and make our products more useful to you.  For more information on how Couchbase collects, protects, and processes information, please refer to the Couchbase Privacy Policy viewable at https://www.couchbase.com/privacy-policy.

### Risks Associated with LLMs

- The use of large language models and similar technology involves risks, including the potential for inaccurate or harmful outputs.
- Couchbase does not review or evaluate the quality or accuracy of such outputs, and such outputs may not reflect Couchbase's views.
- You are solely responsible for determining whether to use large language models and related technology, and for complying with any license terms, terms of use, and your organization's policies governing your use of the same.

### Support Policy

Couchbase MCP Server is open source and maintained by Couchbase engineers. How you get help depends on how you use it:

- **Community use** — report bugs, ask questions and request features by [opening a GitHub issue](https://github.com/couchbase/mcp-server-couchbase/issues). Couchbase engineers monitor the repository and respond on a best-effort basis, with no response-time commitment. The Couchbase support portal cannot help with community use.
- **Enterprise support** — available by licensing [Couchbase AI Data Plane](https://www.couchbase.com/downloads/?family=ai-data-plane), which also covers Couchbase Agent Memory and Couchbase Agent Catalog. Licensed customers get help through their usual Couchbase Support channels.

Thank you for using and contributing to the project!
