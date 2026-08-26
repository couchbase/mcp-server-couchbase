"""Constants and defaults for the Enterprise Analytics streaming MCP server."""

MCP_SERVER_NAME = "enterprise-analytics-streaming-mcp"

# Default Analytics *query* service endpoint. NOTE: this is the query port, not
# the web console. In the local Docker setup the container's 8095 is published
# as host 9095; the console (8091 -> 9091) returns 404 on /api/v1/request.
DEFAULT_EA_ENDPOINT = "http://localhost:9095"

DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

ALLOWED_TRANSPORTS = ["stdio", "http", "sse"]
NETWORK_TRANSPORTS = {"http", "sse"}

# Map user-friendly transport names to FastMCP SDK transport names.
NETWORK_TRANSPORTS_SDK_MAPPING = {"http": "streamable-http"}

# --- Streaming defaults -------------------------------------------------

# Rows returned per tool call. One row per call would cost a full LLM
# inference round-trip per row (10k rows -> 10k tool calls), so we batch by
# default. batch_size=1 is still allowed for true row-at-a-time reads.
DEFAULT_BATCH_SIZE = 10
MAX_BATCH_SIZE = 1000

# Max concurrently open cursors. Each open cursor holds an HTTP connection,
# a thread-pool slot, and the SDK's ~100-row parse-ahead buffer, so this is
# a real resource bound rather than a cosmetic limit.
DEFAULT_MAX_OPEN_STREAMS = 10

# Whole-request deadline for a streaming query. This is NOT an idle timeout:
# it starts when the query is submitted and keeps running while a cursor sits
# paused between tool calls, so a slowly-consumed stream can expire mid-read.
DEFAULT_QUERY_TIMEOUT_SECONDS = 600

# Idle TTL after which an untouched cursor is reaped, and how often the
# background sweeper runs. The SDK's own timeout only trips inside
# get_next_row(), which an abandoned cursor never calls again -- so without
# this sweeper such a cursor would hold its socket until process exit.
DEFAULT_CURSOR_IDLE_TTL_SECONDS = 600
CURSOR_REAPER_INTERVAL_SECONDS = 60
