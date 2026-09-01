# Enterprise Analytics MCP Server

A small MCP server that gives an LLM tools to explore and query a Couchbase Enterprise Analytics (EA) cluster.

## Prerequisites

- Python 3.10–3.14
- [uv](https://docs.astral.sh/uv/)
- A running EA cluster you can reach (connection string, username, password)

## 1. Install

From the `analytics-mcp` directory:

```bash
cd analytics-mcp
uv sync --extra dev
```

## 2. Try it standalone

```bash
EA_CONNECTION_STRING=http://localhost:8095 \
EA_USERNAME=Administrator \
EA_PASSWORD=password \
uv run ea-mcp-server
```

If it connects, the server starts and waits on stdio — that's expected. Ctrl-C to stop.

## 3. Add it to Claude Desktop

Open Claude Desktop's config file (`Settings → Developer → Edit Config`, or
directly at `~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS) and add an entry under `mcpServers`:

```json
{
  "mcpServers": {
    "couchbase-enterprise-analytics": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/mcp-server-couchbase/analytics-mcp",
        "ea-mcp-server"
      ],
      "env": {
        "EA_CONNECTION_STRING": "http://localhost:8091",
        "EA_USERNAME": "Administrator",
        "EA_PASSWORD": "password"
      }
    }
  }
}
```

Replace `--directory` with the actual absolute path on your machine, and swap
in your cluster's real connection string/credentials. Restart Claude Desktop
and the tools below should show up under the 🔨 tools menu.

## Tools

| Tool | Description |
| --- | --- |
| `get_databases_in_cluster` | List all databases in the cluster |
| `get_scopes_in_database` | List all scopes in a database |
| `get_collections_in_scope` | List all collections (datasets) in a scope |
| `get_schema_for_collection` | Infer a collection's JSON schema by sampling documents |
| `create_index` | Create a secondary index via `CREATE INDEX` |
| `run_query_sync` | Run a SQL++ statement and return all result rows |

## Note

This is a prototype: no OAuth, no scope enforcement, no read-only mode. Every
tool above is always registered and available.
