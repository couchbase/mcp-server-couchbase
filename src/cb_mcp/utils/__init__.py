"""
Couchbase MCP Utilities

This module contains utility functions for configuration, connection, and context management.
"""

# CLI adapters
from .cli import (
    validate_log_level,
    validate_log_path,
    validate_log_sinks,
    validate_scope_label,
    validate_stateless_http,
)

# Tool-execution concurrency
from .concurrency import apply_thread_pool_limit

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
    DEFAULT_WORKERS,
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

# Multi-worker (multi-process) support
from .multiprocess import (
    ALLOWED_STATELESS_HTTP_VALUES,
    WORKER_APP_IMPORT_STRING,
    ParsedStatelessHttp,
    WorkerConfigError,
    export_worker_config,
    load_worker_config,
    resolve_worker_settings,
    uvicorn_log_level,
    worker_log_file,
)

# OAuth scope enforcement
from .scope_enforcement import required_scopes_for_tool, wrap_with_scope_check

# Reo.dev telemetry
from .telemetry import send_install_ping, wrap_with_telemetry

# Note: Individual modules create their own hierarchical loggers using:
# logger = logging.getLogger(f"{MCP_SERVER_NAME}.module.name")

__all__ = [
    # Config
    "get_settings",
    "parse_tool_names",
    # Tool-execution concurrency
    "apply_thread_pool_limit",
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
    "DEFAULT_WORKERS",
    "ALLOWED_TRANSPORTS",
    "NETWORK_TRANSPORTS",
    "NETWORK_TRANSPORTS_SDK_MAPPING",
    # Multi-worker support
    "ALLOWED_STATELESS_HTTP_VALUES",
    "WORKER_APP_IMPORT_STRING",
    "ParsedStatelessHttp",
    "WorkerConfigError",
    "export_worker_config",
    "load_worker_config",
    "resolve_worker_settings",
    "uvicorn_log_level",
    "worker_log_file",
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
    "validate_scope_label",
    "validate_stateless_http",
    # Elicitation
    "wrap_with_confirmation",
    # Environment diagnostics
    "log_environment_info",
    "STREAMABLE_HTTP_TRANSPORT",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "ALLOWED_OAUTH_ALGORITHMS",
    "DEFAULT_OAUTH_ALGORITHM",
    # OAuth scope enforcement
    "required_scopes_for_tool",
    "wrap_with_scope_check",
    # Reo.dev telemetry
    "send_install_ping",
    "wrap_with_telemetry",
]
