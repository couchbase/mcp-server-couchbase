# Enterprise Analytics Streaming MCP Server (POC)

A FastMCP server exposing Couchbase Enterprise Analytics' **row-streaming**
query API as four MCP tools, so an LLM can walk a very large result set a batch
at a time instead of loading it all into memory.

Companion POC to `async_pocs/mcp-server-ea-sdk`, which covers the *Server Async
Request API* (`start_query` / `QueryHandle`). This one covers
`cluster.execute_query().rows()`.

## Tools

| Tool | SDK path | Purpose |
|---|---|---|
| `stream_query_results(statement, batch_size=10)` | `execute_query().rows()` + N x `next()` | Open a stream, return the first batch and a `cursor` token |
| `fetch_next_rows(cursor, batch_size=None)` | N x `next()` on the stored iterator | Resume and pull the next batch |
| `close_query_stream(cursor)` | `result.cancel()` | Stop early and free the connection |
| `list_query_streams()` | registry read | Recover a lost cursor token |

### Response shape

```json
{
  "cursor": "a3f...",
  "rows": [ ... ],
  "rows_in_batch": 10,
  "rows_so_far": 30,
  "done": false,
  "next": "fetch_next_rows"
}
```

The final batch sets `done: true`, adds `metadata` (`request_id`,
`result_count`, `result_size`), and closes the cursor automatically.

## How it works

MCP tool calls are independent invocations and can only carry *strings* between
them, but streaming requires the live `BlockingQueryResult` /
`BlockingIterator` pair, which wraps an open socket and a background parse
thread and cannot be serialized. So the live objects stay in a server-side
`CursorRegistry` keyed by an opaque UUID, and only the token goes to the
client — a coat-check ticket, with the server holding the coat.

`BlockingIterator.__next__` calls `get_next_row()`, which pulls one row off the
open response. Nothing forces the loop to run to completion, so the iterator can
be advanced N times, returned from, and advanced again on a later tool call.
That is what makes a resumable cursor possible.

## Behaviour and limits

**Batching, not one row per call.** `batch_size` defaults to 10. One row per
call would cost a full LLM inference round-trip per row (10k rows -> 10k tool
calls). `batch_size: 1` still gives strict row-at-a-time reads.

**Cursors are forward-only.** The SDK keeps no history of served rows, so a
cursor cannot be rewound. Reading row 4 after row 3 works; going back to row 2
does not. Retain rows you still need, or re-run the query.

**Cursors are independent and interleavable.** Each `stream_query_results` call
creates its own result, iterator, and socket under its own token. Multiple
cursors can be open at once and read in any order — reading from cursor B does
not disturb cursor A's position.

**A paused cursor is not free.** The SDK parses ahead on a background thread
into a queue bounded by `buffered_row_max` (100 rows, backpressure at 75%), plus
a 64KB HTTP byte buffer. So an idle cursor costs roughly *100 rows + 64KB + one
socket + one thread-pool slot*, regardless of `batch_size`. With
`--max-open-streams 10` the ceiling is ~1000 buffered rows. Still bounded, and
far cheaper than `get_all_rows()` on a huge result — but not zero.

**`--query-timeout` is a whole-request deadline, not an idle timeout.** It
starts when the query is submitted and keeps running while a cursor sits paused
between tool calls, so a stream consumed more slowly than the timeout expires
mid-iteration. When that happens the tool returns
`{"error": "stream_expired", "rows_so_far": N, "done": true}` rather than an SDK
traceback. Streaming cannot resume from a partial read; the query must be re-run.
Multiple open cursors each run their own clock **concurrently** — working on
cursor B does not pause cursor A's deadline.

**Memory is reclaimed on exhaustion, but abandonment needs the reaper.** When
the last row is read, `StopIteration` triggers `set_metadata()` and a
`finally: close()` in the SDK, releasing the socket; dropping the registry entry
then frees the iterator and its buffer. But the SDK enforces `query_timeout`
*inside* `get_next_row()` via a polled state check, not a timer — a cursor the
client never returns to never trips it and would hold its socket until process
exit. The `CursorReaper` daemon thread sweeps every 60s and cancels cursors idle
past `--cursor-idle-ttl`. Lifespan shutdown cancels whatever remains.

**Single process only.** The registry is in-memory and attached to
`AppContext`. Correct for stdio and single-replica HTTP; a token minted on
replica A is unknown to replica B, and a restart wipes the map. Unlike the
async-handle POC, there is no stateless alternative — streaming is inherently
connection-bound, so a resumable cursor across replicas would mean re-running
the query with an `OFFSET`.

## Configuration

| Flag | Env | Default | Notes |
|---|---|---|---|
| `--endpoint` | `EA_ENDPOINT` | `http://localhost:9095` | Query port, not the web console |
| `--username` | `EA_USERNAME` | — | |
| `--password` | `EA_PASSWORD` | — | |
| `--query-timeout` | `EA_QUERY_TIMEOUT` | `600` | Whole-request deadline (seconds) |
| `--max-open-streams` | `EA_MAX_OPEN_STREAMS` | `10` | Concurrent cursor cap |
| `--cursor-idle-ttl` | `EA_CURSOR_IDLE_TTL` | `600` | Idle seconds before reaping |
| `--transport` | `EA_MCP_TRANSPORT` | `stdio` | `stdio`, `http`, `sse` |
| `--host` / `--port` | `EA_MCP_HOST` / `EA_MCP_PORT` | `127.0.0.1` / `8000` | Network transports only |

## Running

```bash
uv venv && uv pip install .
EA_USERNAME=... EA_PASSWORD=... enterprise-analytics-streaming-mcp-server \
  --endpoint http://localhost:9095
```

Or in `.mcp.json`:

```json
{
  "mcpServers": {
    "enterprise-analytics-streaming": {
      "command": "uv",
      "args": ["--directory", "streaming_pocs/mcp-server-ea-sdk", "run",
               "enterprise-analytics-streaming-mcp-server"],
      "env": { "EA_ENDPOINT": "http://localhost:9095",
               "EA_USERNAME": "...", "EA_PASSWORD": "..." }
    }
  }
}
```

## Example flow

```
stream_query_results("SELECT * FROM airline", batch_size=3)
  -> cursor=a3f, rows[0:3],  rows_so_far=3,  done=false

stream_query_results("SELECT * FROM hotel", batch_size=1)
  -> cursor=b7c, rows[0:1],  rows_so_far=1,  done=false

fetch_next_rows("a3f")            # cursor A resumes at row 4
  -> rows[3:13], rows_so_far=13, done=false

fetch_next_rows("b7c")            # cursor B unaffected by A
  -> rows[1:11], rows_so_far=11, done=false

close_query_stream("b7c")         # done with B early
  -> closed=true, rows_served=11

fetch_next_rows("a3f")            # ... until exhausted
  -> rows[...], done=true, metadata={result_count: ...}
```

## Layout

```
src/
├── mcp_server.py                 # CLI, lifespan, tool registration
├── providers/static.py           # single cluster for the server's life
└── ea_mcp/
    ├── core/contracts.py         # EAClusterProvider protocol
    ├── utils/
    │   ├── constants.py          # defaults incl. streaming knobs
    │   ├── connection.py         # cluster + query_timeout wiring
    │   ├── context.py            # AppContext, accessors
    │   ├── cursor_registry.py    # token -> live cursor, reap/close
    │   └── reaper.py             # idle-cursor sweeper thread
    └── tools/streaming_query.py  # the four tools
```

Tools are plain `def`, not `async def`: `get_next_row()` blocks on a socket, so
it must run on FastMCP's thread pool rather than stalling the event loop.
