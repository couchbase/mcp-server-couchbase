# Enterprise Analytics MCP Server

A [FastMCP](https://github.com/jlowin/fastmcp) server exposing Couchbase
**Enterprise Analytics** (EA) Server Async Request API as MCP tools. EA is a
separate product from the operational Couchbase Server and uses its own SDK
(`couchbase-analytics`).

## Tools (Server Async Request API — EA 2.2+, SDK ≥ 1.1.0)

| Tool | SDK call | Description |
|------|----------|-------------|
| `run_query_async` | `cluster.start_query()` | Submit a long-running query; returns an opaque `query_handle` token immediately (does not wait). |
| `get_async_query_status` | `handle.fetch_status().results_ready()` | Report whether results are ready. |
| `get_async_query_results` | `status.result_handle().fetch_results()` | Retrieve rows + metadata once ready. |
| `discard_async_query_results` | `result_handle.discard_results()` | Release server-side buffers without fetching. |
| `cancel_async_query` | `handle.cancel()` | Cancel the running query. |
| `list_async_queries` | registry listing | Recover tracked `query_handle` tokens (e.g. if one was lost from context). Lists only queries tracked by **this** process. |

## How handles are tracked

The live SDK handle objects cannot be serialized (they hold a thread lock) or
sent to the client, so `run_query_async` stores the live handle in a
**server-side registry** and returns an opaque UUID `query_handle` token. Every
other tool passes that token back to look the handle up.

This registry lives in one process's memory, so it is correct for **stdio** and
**single-replica HTTP**. It is *not* sufficient for multi-replica HTTP or
surviving a restart — those require a stateless backend that carries EA's
server-side request-id/handle strings and rebuilds the REST calls each time.
The registry is deliberately isolated (`ea_mcp/utils/handle_registry.py`) so
such a backend can replace it behind the same method surface.

## Requirements

- Python ≥ 3.10 (the analytics SDK 1.1.0 requires it)
- A running Enterprise Analytics cluster (2.2+)
- `couchbase-analytics >= 1.1.0`

## Configuration

| Flag | Env var | Default | Notes |
|------|---------|---------|-------|
| `--endpoint` | `EA_ENDPOINT` | `http://localhost:9095` | The **query** service port, not the console (`9091`). |
| `--username` | `EA_USERNAME` | — | |
| `--password` | `EA_PASSWORD` | — | |
| `--transport` | `EA_MCP_TRANSPORT` | `stdio` | `stdio`, `http`, or `sse`. |
| `--host` | `EA_MCP_HOST` | `127.0.0.1` | Network transports only. |
| `--port` | `EA_MCP_PORT` | `8000` | Network transports only. |

## Run

```bash
pip install -e .

EA_ENDPOINT=http://localhost:9095 \
EA_USERNAME=Administrator \
EA_PASSWORD=password \
enterprise-analytics-mcp-server --transport stdio
```

Or directly:

```bash
python src/mcp_server.py --endpoint http://localhost:9095 \
  --username Administrator --password password
```
