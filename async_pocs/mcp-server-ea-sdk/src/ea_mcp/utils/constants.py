"""Constants and defaults for the Enterprise Analytics MCP server."""

MCP_SERVER_NAME = "enterprise-analytics-mcp"

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
