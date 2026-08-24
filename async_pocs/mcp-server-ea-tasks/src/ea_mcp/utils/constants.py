"""Constants and defaults for the Tasks-based EA MCP server."""

MCP_SERVER_NAME = "enterprise-analytics-tasks-mcp"

# Analytics query service endpoint (query port, not the web console).
DEFAULT_EA_ENDPOINT = "http://localhost:9095"

DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

ALLOWED_TRANSPORTS = ["stdio", "http", "sse"]
NETWORK_TRANSPORTS = {"http", "sse"}
NETWORK_TRANSPORTS_SDK_MAPPING = {"http": "streamable-http"}
