"""
Couchbase MCP Server
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import click
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

# Reusable tools and utilities from the cb_mcp package
from cb_mcp.auth import OAuthConfigError, resolve_oauth
from cb_mcp.tool_registration import prepare_tools_for_registration
from cb_mcp.tools import TOOL_ANNOTATIONS
from cb_mcp.utils import (
    ALLOWED_OAUTH_ALGORITHMS,
    ALLOWED_TRANSPORTS,
    DEFAULT_HOST,
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_LEVEL,
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
    AppContext,
    configure_logging,
    get_resolved_logging_config,
    log_environment_info,
    validate_log_level,
    validate_log_path,
    validate_log_sinks,
    validate_scope_label,
)

# Standalone-host provider implementation
from providers.static import StaticClusterProvider

logger = logging.getLogger(MCP_SERVER_NAME)


@click.command(context_settings={"show_default": True})
@click.option(
    "--connection-string",
    envvar="CB_CONNECTION_STRING",
    help="Couchbase connection string (required for operations)",
)
@click.option(
    "--username",
    envvar="CB_USERNAME",
    help="Couchbase database user (required for operations)",
)
@click.option(
    "--password",
    envvar="CB_PASSWORD",
    help="Couchbase database password (required for operations)",
)
@click.option(
    "--ca-cert-path",
    envvar="CB_CA_CERT_PATH",
    help="Path to the server trust store (CA certificate) file. The certificate at this path is used to verify the server certificate during the authentication process.",
)
@click.option(
    "--client-cert-path",
    envvar="CB_CLIENT_CERT_PATH",
    help="Path to the client certificate file used for mTLS authentication.",
)
@click.option(
    "--client-key-path",
    envvar="CB_CLIENT_KEY_PATH",
    help="Path to the client certificate key file used for mTLS authentication.",
)
@click.option(
    "--read-only-mode",
    envvar="CB_MCP_READ_ONLY_MODE",
    type=bool,
    default=DEFAULT_READ_ONLY_MODE,
    help="Enable read-only mode. When True, all write operations (KV and Query) are disabled and KV write tools are not loaded. Set to False to enable write operations.",
)
@click.option(
    "--transport",
    envvar=["CB_MCP_TRANSPORT"],
    type=click.Choice(ALLOWED_TRANSPORTS),
    default=DEFAULT_TRANSPORT,
    help="Transport mode for the server (stdio, http or sse). Default is stdio. OAuth is only honored with http (streamable-http).",
)
@click.option(
    "--host",
    envvar="CB_MCP_HOST",
    default=DEFAULT_HOST,
    help="Host to run the server on.",
)
@click.option(
    "--port",
    envvar="CB_MCP_PORT",
    default=DEFAULT_PORT,
    help="Port to run the server on.",
)
@click.option(
    "--disabled-tools",
    "disabled_tools",
    envvar="CB_MCP_DISABLED_TOOLS",
    help="Tools to disable. Accepts comma-separated tool names (e.g., 'tool_1,tool_2') "
    "or a file path containing one tool name per line.",
)
@click.option(
    "--confirmation-required-tools",
    "confirmation_required_tools",
    envvar="CB_MCP_CONFIRMATION_REQUIRED_TOOLS",
    help="Comma-separated tool names that require user confirmation before execution. "
    "Also accepts a file path containing one tool name per line. "
    "Requires the MCP client to support elicitation.",
)
@click.option(
    "--log-level",
    envvar="CB_MCP_LOG_LEVEL",
    default=DEFAULT_LOG_LEVEL,
    callback=validate_log_level,
    help="Logging level for MCP server and Couchbase SDK. Allowed values: "
    "off, debug, info, warning, error. Use 'off' to disable logging "
    "entirely. Invalid values fall back to the default with an error "
    "log entry.",
)
@click.option(
    "--log-sinks",
    envvar="CB_MCP_LOG_SINKS",
    default=DEFAULT_LOG_SINKS,
    callback=validate_log_sinks,
    help="Comma-separated list of log sinks. Allowed values: stderr, file. "
    "Include 'file' (optionally with --log-file) to write per-level files; "
    "include 'stderr' to write to the console.",
)
@click.option(
    "--log-file",
    envvar="CB_MCP_LOG_FILE",
    default=DEFAULT_LOG_FILE,
    callback=validate_log_path,
    help="Base file path for the per-level log files. One rotating file is written "
    "per level, derived by inserting the level name: e.g. mcp_server.log -> "
    "mcp_server.debug.log, mcp_server.info.log, mcp_server.warning.log, "
    "mcp_server.error.log (the error file also captures CRITICAL). Only active "
    "when 'file' is in --log-sinks.",
)
@click.option(
    "--log-rotation-max-size-mb",
    envvar="CB_MCP_LOG_ROTATION_MAX_SIZE_MB",
    # Default None so the 1 MB default is applied only when neither this nor the
    # deprecated --log-max-bytes is set.
    type=click.FloatRange(min=0),
    default=None,
    help="Global maximum size in MB per-level log file before it rotates, "
    "inherited by every level unless overridden. Default is 1 MB. 0 is invalid "
    "and falls back to the default with a startup warning.",
)
@click.option(
    "--log-max-bytes",
    envvar="CB_MCP_LOG_MAX_BYTES",
    # DEPRECATED: superseded by --log-rotation-max-size-mb (MB). Still honored in
    # bytes for backward compatibility. Default None so it's only applied when
    # explicitly set; if set alongside --log-rotation-max-size-mb it is ignored.
    type=click.IntRange(min=0),
    default=None,
    help="[DEPRECATED] Global rotation size in bytes; use --log-rotation-max-size-mb "
    "(MB) instead. Still honored for backward compatibility. Ignored when "
    "--log-rotation-max-size-mb is also set. 0 is invalid and falls back to the "
    "default with a startup warning.",
)
@click.option(
    "--log-error-rotation-max-size-mb",
    envvar="CB_MCP_LOG_ERROR_ROTATION_MAX_SIZE_MB",
    type=click.FloatRange(min=0),
    default=None,
    help="Rotation size in MB for the ERROR log file. Overrides "
    "--log-rotation-max-size-mb for ERROR; inherits it when unset. 0 is invalid and "
    "falls back to the inherited global with a startup warning.",
)
@click.option(
    "--log-warning-rotation-max-size-mb",
    envvar="CB_MCP_LOG_WARNING_ROTATION_MAX_SIZE_MB",
    type=click.FloatRange(min=0),
    default=None,
    help="Rotation size in MB for the WARNING log file. Overrides "
    "--log-rotation-max-size-mb for WARNING; inherits it when unset. 0 is invalid "
    "and falls back to the inherited global with a startup warning.",
)
@click.option(
    "--log-info-rotation-max-size-mb",
    envvar="CB_MCP_LOG_INFO_ROTATION_MAX_SIZE_MB",
    type=click.FloatRange(min=0),
    default=None,
    help="Rotation size in MB for the INFO log file. Overrides "
    "--log-rotation-max-size-mb for INFO; inherits it when unset. 0 is invalid and "
    "falls back to the inherited global with a startup warning.",
)
@click.option(
    "--log-debug-rotation-max-size-mb",
    envvar="CB_MCP_LOG_DEBUG_ROTATION_MAX_SIZE_MB",
    type=click.FloatRange(min=0),
    default=None,
    help="Rotation size in MB for the DEBUG log file. Overrides "
    "--log-rotation-max-size-mb for DEBUG; inherits it when unset. 0 is invalid and "
    "falls back to the inherited global with a startup warning.",
)
@click.option(
    "--log-retention-backup-count",
    envvar="CB_MCP_LOG_RETENTION_BACKUP_COUNT",
    # 0 keeps no rotated backups (only the live file); negative is rejected.
    type=click.IntRange(min=0),
    default=DEFAULT_LOG_BACKUP_COUNT,
    help="Number of rotated backup files kept per-level log file, excluding "
    "the live file. Applies to every level unless overridden per level. Set to 0 "
    "to keep only the live file.",
)
@click.option(
    "--log-error-retention-backup-count",
    envvar="CB_MCP_LOG_ERROR_RETENTION_BACKUP_COUNT",
    type=click.IntRange(min=0),
    default=None,
    help="Rotated backups kept for the ERROR log file. Overrides "
    "--log-retention-backup-count for ERROR; inherits it when unset.",
)
@click.option(
    "--log-warning-retention-backup-count",
    envvar="CB_MCP_LOG_WARNING_RETENTION_BACKUP_COUNT",
    type=click.IntRange(min=0),
    default=None,
    help="Rotated backups kept for the WARNING log file. Overrides "
    "--log-retention-backup-count for WARNING; inherits it when unset.",
)
@click.option(
    "--log-info-retention-backup-count",
    envvar="CB_MCP_LOG_INFO_RETENTION_BACKUP_COUNT",
    type=click.IntRange(min=0),
    default=None,
    help="Rotated backups kept for the INFO log file. Overrides "
    "--log-retention-backup-count for INFO; inherits it when unset.",
)
@click.option(
    "--log-debug-retention-backup-count",
    envvar="CB_MCP_LOG_DEBUG_RETENTION_BACKUP_COUNT",
    type=click.IntRange(min=0),
    default=None,
    help="Rotated backups kept for the DEBUG log file. Overrides "
    "--log-retention-backup-count for DEBUG; inherits it when unset.",
)
@click.option(
    "--oauth-jwks-uri",
    envvar="CB_MCP_OAUTH_JWT_JWKS_URI",
    default=None,
    help="JWKS endpoint of the upstream identity provider, used to verify "
    "bearer JWT signatures (e.g. https://auth.example.com/.well-known/jwks.json). "
    "Required to enable OAuth (along with --oauth-issuer and --oauth-audience). "
    "Only honored when --transport=http.",
)
@click.option(
    "--oauth-issuer",
    envvar="CB_MCP_OAUTH_JWT_ISSUER",
    default=None,
    help="Expected JWT 'iss' claim value. Also advertised as the authorization "
    "server in the protected-resource metadata when --oauth-mcp-base-url is set. "
    "Required to enable OAuth.",
)
@click.option(
    "--oauth-audience",
    envvar="CB_MCP_OAUTH_JWT_AUDIENCE",
    default=None,
    help="Expected JWT 'aud' claim value. Required to enable OAuth.",
)
@click.option(
    "--oauth-algorithm",
    envvar="CB_MCP_OAUTH_JWT_ALGORITHM",
    type=click.Choice(ALLOWED_OAUTH_ALGORITHMS),
    default=DEFAULT_OAUTH_ALGORITHM,
    show_default=True,
    help="JWT signing algorithm. One of RS256/384/512, ES256/384/512, PS256/384/512.",
)
@click.option(
    "--oauth-mcp-base-url",
    envvar="CB_MCP_OAUTH_MCP_BASE_URL",
    default=None,
    help="Public base URL of this MCP server (e.g. https://api.yourcompany.com). "
    "When set, the server publishes RFC 9728 Protected Resource Metadata at "
    "<base_url>/.well-known/oauth-protected-resource/mcp so PRM-aware clients "
    "can discover the authorization server and perform DCR directly against it. "
    "Optional — omit to run as a JWT-validating resource server only.",
)
@click.option(
    "--oauth-scope-read-label",
    "oauth_scope_read",
    envvar="CB_MCP_OAUTH_SCOPE_READ_LABEL",
    default=SCOPE_READ,
    callback=validate_scope_label,
    help="Override the OAuth scope label the server treats as 'read' access. "
    "Use this when your IdP cannot emit the canonical scope form. "
    "The configured value is advertised in PRM and accepted in the token "
    "'scope'/'scp' claims. A blank/invalid value warns and falls back to the "
    "default.",
)
@click.option(
    "--oauth-scope-write-label",
    "oauth_scope_write",
    envvar="CB_MCP_OAUTH_SCOPE_WRITE_LABEL",
    default=SCOPE_WRITE,
    callback=validate_scope_label,
    help="Override the OAuth scope label for 'write' access. "
    "Same semantics as --oauth-scope-read-label.",
)
@click.version_option(package_name="couchbase-mcp-server")
@click.pass_context
def main(
    ctx,
    connection_string,
    username,
    password,
    ca_cert_path,
    client_cert_path,
    client_key_path,
    read_only_mode,
    transport,
    host,
    port,
    disabled_tools,
    confirmation_required_tools,
    oauth_jwks_uri,
    oauth_issuer,
    oauth_audience,
    oauth_algorithm,
    oauth_mcp_base_url,
    oauth_scope_read,
    oauth_scope_write,
    log_level,
    log_sinks,
    log_file,
    log_rotation_max_size_mb,
    log_max_bytes,
    log_error_rotation_max_size_mb,
    log_warning_rotation_max_size_mb,
    log_info_rotation_max_size_mb,
    log_debug_rotation_max_size_mb,
    log_retention_backup_count,
    log_error_retention_backup_count,
    log_warning_retention_backup_count,
    log_info_retention_backup_count,
    log_debug_retention_backup_count,
):
    """Couchbase MCP Server"""

    # log_level / log_sinks are the parse results from their Click callbacks:
    # each carries the resolved value plus any rejected input, which is passed
    # to configure_logging so the fallback can be reported once handlers exist.
    # Per-level overrides: keep only the levels the operator set explicitly; the
    # rest inherit the global. Rotation-size overrides are in MB, matching the
    # canonical --log-rotation-max-size-mb global.
    rotation_size_overrides = {
        level: value
        for level, value in (
            ("ERROR", log_error_rotation_max_size_mb),
            ("WARNING", log_warning_rotation_max_size_mb),
            ("INFO", log_info_rotation_max_size_mb),
            ("DEBUG", log_debug_rotation_max_size_mb),
        )
        if value is not None
    }
    backup_count_overrides = {
        level: value
        for level, value in (
            ("ERROR", log_error_retention_backup_count),
            ("WARNING", log_warning_retention_backup_count),
            ("INFO", log_info_retention_backup_count),
            ("DEBUG", log_debug_retention_backup_count),
        )
        if value is not None
    }
    configure_logging(
        level=log_level.level,
        sinks=log_sinks.sinks,
        log_file=log_file,
        log_rotation_max_size_mb=log_rotation_max_size_mb,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_retention_backup_count,
        log_rotation_size_overrides=rotation_size_overrides,
        log_backup_count_overrides=backup_count_overrides,
        invalid_level=log_level.invalid_token,
        invalid_sinks=log_sinks.invalid_tokens,
    )

    try:
        auth = resolve_oauth(
            transport=transport,
            jwks_uri=oauth_jwks_uri,
            issuer=oauth_issuer,
            audience=oauth_audience,
            algorithm=oauth_algorithm,
            base_url=oauth_mcp_base_url,
            scope_read=oauth_scope_read,
            scope_write=oauth_scope_write,
        )
    except OAuthConfigError as e:
        raise click.UsageError(str(e)) from e

    (
        final_tools,
        configured_confirmation_tool_names,
        disabled_tool_names,
    ) = prepare_tools_for_registration(
        read_only_mode=read_only_mode,
        disabled_tools=disabled_tools,
        confirmation_required_tools=confirmation_required_tools,
        enforce_scopes=auth is not None,
    )

    # CLI-resolved configuration lives on AppContext, not in a module global.
    # This lets FastMCP's threadpool workers read it through ``ctx``.
    settings = {
        "connection_string": connection_string,
        "username": username,
        "password": password,
        "ca_cert_path": ca_cert_path,
        "client_cert_path": client_cert_path,
        "client_key_path": client_key_path,
        "read_only_mode": read_only_mode,
        "transport": transport,
        "host": host,
        "port": port,
        # OAuth resource-server config (non-secret IdP coordinates), captured
        # for the env-info diagnostic and get_server_configuration_status.
        # ``oauth_enabled`` is whether OAuth is active: resolve_oauth returns
        # None for non-http transports even when JWT settings are present.
        "oauth_enabled": auth is not None,
        "oauth_jwks_uri": oauth_jwks_uri,
        "oauth_issuer": oauth_issuer,
        "oauth_audience": oauth_audience,
        "oauth_algorithm": oauth_algorithm,
        "oauth_mcp_base_url": oauth_mcp_base_url,
        "oauth_scope_read_label": oauth_scope_read,
        "oauth_scope_write_label": oauth_scope_write,
        "disabled_tools": disabled_tool_names,
        "confirmation_required_tools": configured_confirmation_tool_names,
    }
    ctx.obj = settings

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        """Build the lifespan AppContext with settings captured from the CLI."""
        logger.info(
            f"MCP server initialized in lazy mode for tool discovery. "
            f"Modes: (read_only_mode={read_only_mode})"
        )
        # Diagnostic snapshot for customer support. Filtered at INFO; visible
        # whenever the user runs with --log-level DEBUG.
        log_environment_info(transport, settings)
        # Hand the resolved logging snapshot to AppContext so shared tools
        # (e.g. get_server_configuration_status) can surface it without
        # coupling to our specific logging module.
        resolved_logging = get_resolved_logging_config()
        app_context = AppContext(
            cluster_provider=StaticClusterProvider(settings=settings),
            settings=settings,
            read_only_mode=read_only_mode,
            logging_config=resolved_logging.as_dict() if resolved_logging else None,
        )
        try:
            yield app_context
        except Exception as e:
            logger.error(f"Error in app lifespan: {e}", exc_info=True)
            raise
        finally:
            if app_context.cluster_provider:
                app_context.cluster_provider.close()
            logger.info("Closing MCP server")

    # Map user-friendly transport names to SDK transport names
    sdk_transport = NETWORK_TRANSPORTS_SDK_MAPPING.get(transport, transport)

    mcp = FastMCP(MCP_SERVER_NAME, lifespan=app_lifespan, auth=auth)

    logger.info(
        f"Registering {len(final_tools)} tool(s) with modes (read_only_mode={read_only_mode})"
    )

    # Register tools; FastMCP 3.x add_tool has no annotations kwarg, so wrap first.
    for tool in final_tools:
        annotations = TOOL_ANNOTATIONS.get(tool.__name__)
        tool_obj = FunctionTool.from_function(tool, annotations=annotations)
        mcp.add_tool(tool_obj)

    logger.info(f"Registered {len(final_tools)} tool(s)")

    run_kwargs = {"host": host, "port": port} if transport in NETWORK_TRANSPORTS else {}
    mcp.run(transport=sdk_transport, show_banner=False, **run_kwargs)  # type: ignore


if __name__ == "__main__":
    main()
