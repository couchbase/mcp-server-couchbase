"""
Couchbase MCP Utilities

This module contains utility functions for configuration, connection, and context management.
"""

# CLI adapters
from .cli import validate_log_level, validate_log_path, validate_log_sinks

# Configuration utilities
from .config import (
    get_settings,
    parse_tool_names,
)

# Connection utilities
from .connection import (
    connect_to_bucket,
    connect_to_couchbase_cluster,
)

# Constants
from .constants import (
    ALLOWED_LOG_LEVELS,
    ALLOWED_LOG_SINKS,
    ALLOWED_OAUTH_ALGORITHMS,
    ALLOWED_TRANSPORTS,
    DEFAULT_HOST,
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_FORMAT,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_MAX_BYTES,
    DEFAULT_LOG_SINKS,
    DEFAULT_OAUTH_ALGORITHM,
    DEFAULT_PORT,
    DEFAULT_READ_ONLY_MODE,
    DEFAULT_TRANSPORT,
    MCP_SERVER_NAME,
    NETWORK_TRANSPORTS,
    NETWORK_TRANSPORTS_SDK_MAPPING,
    SCOPE_READ,
    SCOPE_WRITE,
    STREAMABLE_HTTP_TRANSPORT,
)

# Context utilities
from .context import (
    AppContext,
    get_cluster_connection,
    get_cluster_provider,
    get_logging_config,
)

# Elicitation utilities
from .elicitation import wrap_with_confirmation

# Environment diagnostics
from .environment import log_environment_info

# Index utilities
from .index_utils import (
    fetch_indexes_from_rest_api,
)

# Logging
from .logging import (
    ResolvedLoggingConfig,
    configure_logging,
    get_resolved_logging_config,
    parse_log_level,
    parse_log_sinks,
)

# OAuth scope enforcement
from .scope_enforcement import required_scopes_for_tool, wrap_with_scope_check

# Note: Individual modules create their own hierarchical loggers using:
# logger = logging.getLogger(f"{MCP_SERVER_NAME}.module.name")

__all__ = [
    # Config
    "get_settings",
    "parse_tool_names",
    # Connection
    "connect_to_couchbase_cluster",
    "connect_to_bucket",
    # Context
    "AppContext",
    "get_cluster_connection",
    "get_cluster_provider",
    "get_logging_config",
    # Index utilities
    "fetch_indexes_from_rest_api",
    # Constants
    "MCP_SERVER_NAME",
    "DEFAULT_READ_ONLY_MODE",
    "DEFAULT_TRANSPORT",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_LOG_MAX_BYTES",
    "DEFAULT_LOG_BACKUP_COUNT",
    "DEFAULT_LOG_FORMAT",
    "DEFAULT_LOG_SINKS",
    "DEFAULT_LOG_FILE",
    "ALLOWED_LOG_LEVELS",
    "ALLOWED_LOG_SINKS",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ALLOWED_TRANSPORTS",
    "NETWORK_TRANSPORTS",
    "NETWORK_TRANSPORTS_SDK_MAPPING",
    # Logging
    "ResolvedLoggingConfig",
    "configure_logging",
    "get_resolved_logging_config",
    "parse_log_level",
    "parse_log_sinks",
    # CLI adapters
    "validate_log_level",
    "validate_log_path",
    "validate_log_sinks",
    # Elicitation
    "wrap_with_confirmation",
    # Environment diagnostics
    "log_environment_info",
    "STREAMABLE_HTTP_TRANSPORT",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "ALLOWED_OAUTH_ALGORITHMS",
    "DEFAULT_OAUTH_ALGORITHM",
    # Elicitation
    "wrap_with_confirmation",
    # OAuth scope enforcement
    "required_scopes_for_tool",
    "wrap_with_scope_check",
]
