"""Constants and defaults for the stateless EA MCP server."""

MCP_SERVER_NAME = "enterprise-analytics-rest-mcp"

# Default Analytics *query* service endpoint (query port, not the web console).
# Local Docker: container 8095 -> host 9095. The console (9091) 404s here.
DEFAULT_EA_ENDPOINT = "http://localhost:9095"

DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

ALLOWED_TRANSPORTS = ["stdio", "http", "sse"]
NETWORK_TRANSPORTS = {"http", "sse"}
NETWORK_TRANSPORTS_SDK_MAPPING = {"http": "streamable-http"}

# EA Server Async Request REST API paths (verified against EA 2.2 / Docker).
#   start:   POST   /api/v1/request           body {statement, mode:"async"}
#   status:  GET    <status handle URL from start>
#   fetch:   GET    <result handle URL from status>
#   discard: DELETE <result handle URL from status>
#   cancel:  DELETE /api/v1/active_requests?request_id=<requestID>
REQUEST_PATH = "/api/v1/request"
ACTIVE_REQUESTS_PATH = "/api/v1/active_requests"

# Status values EA reports; "success" means results are ready to fetch.
STATUS_READY = "success"
TERMINAL_FAILURE_STATUSES = {"failed", "fatal", "cancelled", "timeout"}
