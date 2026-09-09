"""
Couchbase MCP Server
"""

import logging

import click

# Reusable tools and utilities from the cb_mcp package
from cb_mcp.auth import OAuthConfigError, resolve_oauth
from cb_mcp.core.app import build_app, run_app
from cb_mcp.core.cli import (
    credential_options,
    logging_options,
    oauth_options,
    read_only_option,
    tool_gating_options,
    transport_options,
)
from cb_mcp.servers.operational import SPEC as OPERATIONAL_SPEC
from cb_mcp.tool_registration import prepare_tools_for_registration
from cb_mcp.utils import (
    LOGGER_ROOT,
    configure_logging,
    get_resolved_logging_config,
)

# Standalone-host provider implementation
from providers.static import StaticClusterProvider

logger = logging.getLogger(LOGGER_ROOT)


@click.command(context_settings={"show_default": True})
@credential_options
@read_only_option
@transport_options
@tool_gating_options
@logging_options
@oauth_options
@click.version_option(package_name="couchbase-mcp-server")
def main(
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

    # CLI-resolved configuration lives on AppContext, not in a module global,
    # so FastMCP's threadpool workers can read it through the request context.
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
    # Hand the resolved logging snapshot to the app so shared tools (e.g.
    # get_server_configuration_status) can surface it without coupling to a
    # specific logging module. Safe to read here: configure_logging ran above.
    resolved_logging = get_resolved_logging_config()

    mcp = build_app(
        OPERATIONAL_SPEC,
        tools=final_tools,
        settings=settings,
        # A factory, not an instance: constructing the provider is deferred to
        # lifespan startup so nothing connects during tool discovery.
        provider_factory=lambda: StaticClusterProvider(settings=settings),
        auth=auth,
        read_only_mode=read_only_mode,
        logging_config=resolved_logging.as_dict() if resolved_logging else None,
    )

    run_app(mcp, transport=transport, host=host, port=port)


if __name__ == "__main__":
    main()
