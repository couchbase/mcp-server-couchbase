# MCP Server Constants
MCP_SERVER_NAME = "couchbase"

# Default Configuration Values
DEFAULT_READ_ONLY_MODE = True
DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# Allowed Transport Types
ALLOWED_TRANSPORTS = ["stdio", "http", "sse"]
NETWORK_TRANSPORTS = ["http", "sse"]
NETWORK_TRANSPORTS_SDK_MAPPING = {
    "http": "streamable-http",
    "sse": "sse",
}

# The MCP spec ties OAuth to streamable-HTTP transport specifically (not SSE),
# so we gate the OAuth wiring strictly on this transport name. SSE is a
# network transport but is explicitly out of scope for OAuth in this build.
STREAMABLE_HTTP_TRANSPORT = "http"

# Index Service Configuration
# Cluster major version at which list_indexes prefers the query service over
# the Index Service REST API. From this version, system:indexes exposes the
# original CREATE INDEX statement in metadata.definition, so we query it
# instead of the /getIndexStatus REST endpoint.
QUERY_SERVICE_LIST_INDEXES_MIN_MAJOR_VERSION = 8

# Logging Configuration
# Change this to DEBUG, WARNING, ERROR as needed
DEFAULT_LOG_LEVEL = "INFO"
# Bytes per megabyte. Rotation sizes are configured in MB
# (CB_MCP_LOG_ROTATION_MAX_SIZE and the per-level CB_MCP_LOG_<LEVEL>_ROTATION_MAX_SIZE)
# and converted to bytes with this factor for the handlers.
BYTES_PER_MB = 1024 * 1024
# Default effective rotation size in bytes (1 MB), used when neither the
# canonical CB_MCP_LOG_ROTATION_MAX_SIZE (MB) nor the deprecated
# CB_MCP_LOG_MAX_BYTES (bytes) is set — and as the fallback when either is given
# an invalid value of 0. The canonical variable is in MB and is inherited by
# every level unless overridden per level; CB_MCP_LOG_MAX_BYTES remains honored
# (in bytes) for backward compatibility but is deprecated.
DEFAULT_LOG_MAX_BYTES = 1 * BYTES_PER_MB  # 1 MB
# Default number of rotated backup files kept per level file, applied to every
# level unless overridden. Exposed globally via CB_MCP_LOG_RETENTION_BACKUP_COUNT
# and per level via CB_MCP_LOG_<LEVEL>_RETENTION_BACKUP_COUNT (which inherit this
# global when unset). 0 means no rotated backups — only the live file is kept,
# still capped by DEFAULT_LOG_MAX_BYTES (truncated on rollover, not backed up).
DEFAULT_LOG_BACKUP_COUNT = 1
ALLOWED_LOG_LEVELS = ("OFF", "DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
# ISO 8601 local time with UTC offset (e.g. 2026-06-09T18:08:49+0530).
# Milliseconds are intentionally omitted; we can switch to a sub-second
# format later via a custom Formatter if support diagnostics need it.
DEFAULT_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"
ALLOWED_LOG_SINKS = ("stderr", "file")
DEFAULT_LOG_SINKS = "stderr"
# Base filename used when file logging is active and the caller omits
# --log-file. The per-level files derive from it by inserting the level name
# (mcp_server.log -> mcp_server.info.log, mcp_server.error.log, ...). The CLI
# layer (mcp_server.py) wires this as the --log-file Click default.
DEFAULT_LOG_FILE = "mcp_server.log"

# OAuth Scopes
# Tokens carrying SCOPE_READ may call read-only tools (including SQL++ query,
# which is classified read-only at startup and runtime-gated by
# read_only_mode). Tokens carrying SCOPE_WRITE may call KV mutation
# tools only. Both scopes are required for full access; the model is
# deliberately strict — SCOPE_WRITE alone cannot reach read tools or SQL++.
SCOPE_READ = "couchbase-mcp:read"
SCOPE_WRITE = "couchbase-mcp:write"

# JWT signing algorithms permitted by JWTVerifier (FastMCP supports HS* too,
# but we restrict to asymmetric per spec since JWKS-based verification is the
# intended deployment).
ALLOWED_OAUTH_ALGORITHMS = [
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
]
DEFAULT_OAUTH_ALGORITHM = "RS256"
