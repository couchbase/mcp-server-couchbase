"""
Couchbase MCP Server
"""

import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import click
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.tools import FunctionTool

# Reusable tools and utilities from the cb_mcp package
from cb_mcp.auth import OAuthConfigError, resolve_oauth
from cb_mcp.tool_registration import (
    TextOnlyFunctionTool,
    prepare_tools_for_registration,
)
from cb_mcp.tools import TOOL_ANNOTATIONS
from cb_mcp.utils import (
    ALLOWED_OAUTH_ALGORITHMS,
    ALLOWED_TRANSPORTS,
    DEFAULT_DISABLE_STRUCTURED_OUTPUT,
    DEFAULT_HOST,
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_LEVEL,
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
    WORKER_APP_IMPORT_STRING,
    AppContext,
    WorkerConfigError,
    apply_thread_pool_limit,
    configure_logging,
    export_worker_config,
    get_resolved_logging_config,
    load_worker_config,
    log_environment_info,
    resolve_worker_settings,
    send_install_ping,
    uvicorn_log_level,
    validate_log_level,
    validate_log_path,
    validate_log_sinks,
    validate_scope_label,
    validate_stateless_http,
    worker_log_file,
)

# Standalone-host provider implementation
from providers.static import StaticClusterProvider

logger = logging.getLogger(MCP_SERVER_NAME)


def configure_logging_from_params(
    params: Mapping[str, Any], log_file: str | None = None
) -> None:
    """Wire up logging from the resolved Click params.

    Split out of ``main`` so a ``--workers`` child process, which is spawned as
    a fresh interpreter and never parses the CLI, can replay the parent's
    logging configuration from the serialized params rather than resolving it a
    second, subtly different way.

    ``log_file`` overrides ``params["log_file"]``; workers pass a PID-suffixed
    path so they never share a rotating file handle.
    """
    # log_level / log_sinks are the parse results from their Click callbacks:
    # each carries the resolved value plus any rejected input, which is passed
    # to configure_logging so the fallback can be reported once handlers exist.
    # Per-level overrides: keep only the levels the operator set explicitly; the
    # rest inherit the global. Rotation-size overrides are in MB, matching the
    # canonical --log-rotation-max-size-mb global.
    rotation_size_overrides = {
        level: value
        for level, value in (
            ("ERROR", params["log_error_rotation_max_size_mb"]),
            ("WARNING", params["log_warning_rotation_max_size_mb"]),
            ("INFO", params["log_info_rotation_max_size_mb"]),
            ("DEBUG", params["log_debug_rotation_max_size_mb"]),
        )
        if value is not None
    }
    backup_count_overrides = {
        level: value
        for level, value in (
            ("ERROR", params["log_error_retention_backup_count"]),
            ("WARNING", params["log_warning_retention_backup_count"]),
            ("INFO", params["log_info_retention_backup_count"]),
            ("DEBUG", params["log_debug_retention_backup_count"]),
        )
        if value is not None
    }
    log_level = params["log_level"]
    log_sinks = params["log_sinks"]
    configure_logging(
        level=log_level.level,
        sinks=log_sinks.sinks,
        log_file=params["log_file"] if log_file is None else log_file,
        log_rotation_max_size_mb=params["log_rotation_max_size_mb"],
        log_max_bytes=params["log_max_bytes"],
        log_backup_count=params["log_retention_backup_count"],
        log_rotation_size_overrides=rotation_size_overrides,
        log_backup_count_overrides=backup_count_overrides,
        invalid_level=log_level.invalid_token,
        invalid_sinks=log_sinks.invalid_tokens,
    )


def resolve_oauth_from_params(params: Mapping[str, Any]):
    """Resolve the OAuth options out of a params mapping.

    Wraps :func:`cb_mcp.auth.resolve_oauth` so the server build and the
    supervisor's fail-fast validation cannot drift apart on which params feed
    it. Raises ``OAuthConfigError`` on an incomplete configuration.
    """
    return resolve_oauth(
        transport=params["transport"],
        jwks_uri=params["oauth_jwks_uri"],
        issuer=params["oauth_issuer"],
        audience=params["oauth_audience"],
        algorithm=params["oauth_algorithm"],
        base_url=params["oauth_mcp_base_url"],
        scope_read=params["oauth_scope_read"],
        scope_write=params["oauth_scope_write"],
    )


def build_mcp_server(params: Mapping[str, Any]) -> FastMCP:
    """Build the FastMCP instance described by the resolved Click params.

    Shared by the single-process path and by every ``--workers`` child, so both
    register the same tools, enforce the same modes, and expose the same
    configuration to ``get_server_configuration_status``.

    Reads two keys that ``main`` resolves before calling: ``stateless_http``
    (already a bool, never ``None``) and the optional ``send_startup_ping``,
    which the ``--workers`` supervisor sets to False so one startup telemetry
    event is emitted for the deployment instead of one per worker.

    Raises ``OAuthConfigError`` when the OAuth options are incomplete; ``main``
    converts that into a usage error.
    """
    transport = params["transport"]
    read_only_mode = params["read_only_mode"]
    disable_structured_output = params["disable_structured_output"]

    auth = resolve_oauth_from_params(params)

    (
        final_tools,
        configured_confirmation_tool_names,
        disabled_tool_names,
    ) = prepare_tools_for_registration(
        read_only_mode=read_only_mode,
        disabled_tools=params["disabled_tools"],
        confirmation_required_tools=params["confirmation_required_tools"],
        enforce_scopes=auth is not None,
    )

    # CLI-resolved configuration lives on AppContext, not in a module global.
    # This lets FastMCP's threadpool workers read it through ``ctx``.
    settings = {
        "connection_string": params["connection_string"],
        "username": params["username"],
        "password": params["password"],
        "ca_cert_path": params["ca_cert_path"],
        "client_cert_path": params["client_cert_path"],
        "client_key_path": params["client_key_path"],
        "read_only_mode": read_only_mode,
        "transport": transport,
        "host": params["host"],
        "port": params["port"],
        # Serving topology. ``workers`` is the size of the process group this
        # server belongs to, so an operator reading the diagnostic can tell a
        # single-process deployment from one worker of several.
        "workers": params["workers"],
        "stateless_http": params["stateless_http"],
        # Replaced during lifespan startup with the limit that actually took
        # effect, so this reports the real ceiling rather than the configured
        # one (which is None whenever the operator left it to the runtime).
        "thread_pool_size": params["thread_pool_size"],
        # Whether tools were registered without an output schema, so results
        # carry text content only. Reported because it changes the shape of
        # every tool response a client sees.
        "disable_structured_output": disable_structured_output,
        # OAuth resource-server config (non-secret IdP coordinates), captured
        # for the env-info diagnostic and get_server_configuration_status.
        # ``oauth_enabled`` is whether OAuth is active: resolve_oauth returns
        # None for non-http transports even when JWT settings are present.
        "oauth_enabled": auth is not None,
        "oauth_jwks_uri": params["oauth_jwks_uri"],
        "oauth_issuer": params["oauth_issuer"],
        "oauth_audience": params["oauth_audience"],
        "oauth_algorithm": params["oauth_algorithm"],
        "oauth_mcp_base_url": params["oauth_mcp_base_url"],
        "oauth_scope_read_label": params["oauth_scope_read"],
        "oauth_scope_write_label": params["oauth_scope_write"],
        "disabled_tools": disabled_tool_names,
        "confirmation_required_tools": configured_confirmation_tool_names,
    }
    send_startup_ping = params.get("send_startup_ping", True)

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        """Build the lifespan AppContext with settings captured from the CLI."""
        logger.info(
            f"MCP server initialized in lazy mode for tool discovery. "
            f"Modes: (read_only_mode={read_only_mode})"
        )
        # Tool calls run on the AnyIO thread pool, so its size caps how many
        # execute concurrently in this process. Applied here because the limiter
        # is run-scoped: it exists only inside the event loop, and each worker
        # process has its own. Record what took effect before the diagnostic is
        # written so support sees the real ceiling.
        settings["thread_pool_size"] = apply_thread_pool_limit(
            params["thread_pool_size"]
        )
        logger.info(
            f"Tool-call concurrency limit: {settings['thread_pool_size']} per process"
        )
        # Diagnostic snapshot for customer support. Filtered at INFO; visible
        # whenever the user runs with --log-level DEBUG.
        log_environment_info(transport, settings)
        if send_startup_ping:
            send_install_ping(transport)
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

    mcp = FastMCP(MCP_SERVER_NAME, lifespan=app_lifespan, auth=auth)

    logger.info(
        f"Registering {len(final_tools)} tool(s) with modes (read_only_mode={read_only_mode})"
    )

    # Disabling structured output takes both halves: ``output_schema=None``
    # stops FastMCP deriving a schema from the tool's return annotation, and
    # TextOnlyFunctionTool drops the structuredContent it would otherwise still
    # attach to dict results. Left at FastMCP's ``NotSet`` default the schema is
    # inferred as usual, so the kwarg is omitted rather than passed as a
    # default.
    tool_class = TextOnlyFunctionTool if disable_structured_output else FunctionTool
    schema_kwargs: dict[str, Any] = (
        {"output_schema": None} if disable_structured_output else {}
    )
    if disable_structured_output:
        logger.info(
            "Structured output disabled: registering tools without an output "
            "schema, so results are returned as text content only."
        )

    # Register tools; FastMCP 3.x add_tool has no annotations kwarg, so wrap first.
    for tool in final_tools:
        annotations = TOOL_ANNOTATIONS.get(tool.__name__)
        tool_obj = tool_class.from_function(
            tool, annotations=annotations, **schema_kwargs
        )
        mcp.add_tool(tool_obj)

    logger.info(f"Registered {len(final_tools)} tool(s)")

    return mcp


def create_app() -> StarletteWithLifespan:
    """ASGI application factory, called once inside each worker process.

    Referenced by import string (``mcp_server:create_app``) because Uvicorn
    spawns workers as fresh interpreters and so cannot inherit an already-built
    app object. The worker recovers the parent's resolved configuration from the
    environment, re-establishes logging under its own PID, and builds its own
    FastMCP instance — including, on first tool call, its own Couchbase cluster
    connection.

    Not a supported entrypoint for an external ASGI server: without the
    configuration the ``--workers`` supervisor publishes, this raises.
    """
    params = load_worker_config()
    configure_logging_from_params(
        params, log_file=worker_log_file(params["log_file"], os.getpid())
    )
    logger.info(
        f"Worker process {os.getpid()} starting "
        f"(of {params['workers']} worker(s), stateless_http=True)"
    )
    mcp = build_mcp_server(params)
    # Session state cannot be shared between processes, so multi-worker mode is
    # always stateless; resolve_worker_settings has already rejected any
    # combination that says otherwise.
    return mcp.http_app(stateless_http=True)


def run_workers(params: Mapping[str, Any]) -> None:
    """Serve the streamable HTTP transport from a group of worker processes.

    Publishes the resolved configuration for the workers to pick up, then hands
    process supervision to Uvicorn: it binds the listening socket once, spawns
    ``workers`` children that accept from it, restarts any that die, and
    forwards shutdown signals. The kernel load-balances connections across the
    children, so this needs no reverse proxy in front.
    """
    workers = params["workers"]
    logger.info(
        f"Starting {workers} worker process(es) on "
        f"{params['host']}:{params['port']} (stateless HTTP)"
    )
    export_worker_config(params)
    uvicorn.run(
        WORKER_APP_IMPORT_STRING,
        factory=True,
        host=params["host"],
        port=params["port"],
        workers=workers,
        # Our AppContext (and therefore the Couchbase connection) is created by
        # the app's lifespan, so it must run in every worker.
        lifespan="on",
        # Matches the value FastMCP uses for its own single-process server.
        timeout_graceful_shutdown=2,
        log_level=uvicorn_log_level(params["log_level"].level),
    )


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
    "--workers",
    envvar="CB_MCP_WORKERS",
    type=click.IntRange(min=1),
    default=DEFAULT_WORKERS,
    help="Number of server worker processes. One process is limited to about "
    "one CPU core by the Python GIL, so raise this to use more cores; a good "
    "starting point is the number of cores available to the server. Values "
    "above 1 require --transport=http and run in stateless HTTP mode. The "
    "workers share one listening socket, so --host/--port are unchanged.",
)
@click.option(
    "--thread-pool-size",
    envvar="CB_MCP_THREAD_POOL_SIZE",
    type=click.IntRange(min=1),
    default=None,
    help="Maximum number of tool calls executed concurrently per worker "
    "process. Tool calls run on a thread pool; requests past this limit wait "
    "for a slot. Raise it when calls spend their time waiting on the cluster "
    "(high-latency links) or when slow tools delay fast ones queued behind "
    "them. It does not raise CPU-bound throughput — one process is limited to "
    "about one core either way; use --workers for that. Unset leaves the AnyIO "
    "default (40). With --workers N the effective total is N times this value.",
)
@click.option(
    "--stateless-http",
    "stateless_http",
    envvar="CB_MCP_STATELESS_HTTP",
    default=None,
    callback=validate_stateless_http,
    help="Handle each HTTP request with a fresh MCP transport instead of "
    "keeping per-session state in the server. Defaults to True when "
    "--workers is above 1 (required, since sessions are not shared between "
    "worker processes) and False otherwise. Only honored with "
    "--transport=http. An unrecognised value falls back to that default with "
    "an error log entry, and a value that cannot work with the rest of the "
    "configuration is overridden with a warning rather than rejected.",
)
@click.option(
    "--disable-structured-output",
    "disable_structured_output",
    envvar="CB_MCP_DISABLE_STRUCTURED_OUTPUT",
    type=bool,
    default=DEFAULT_DISABLE_STRUCTURED_OUTPUT,
    help="Register every tool without an output schema, so results are "
    "returned as text content only instead of also carrying structured "
    "content. Use this with clients that mishandle or reject a tool's "
    "structured output, or to avoid sending each result twice. Applies to all "
    "tools; it cannot be set per tool.",
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
    "off, debug, info, warning, error. Use 'off' to disable logging entirely. Invalid values fall "
    "back to the default with an error log entry.",
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
    workers,
    thread_pool_size,
    stateless_http,
    disable_structured_output,
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
    """Couchbase MCP Server

    The options are declared above and resolved by Click into ``ctx.params``.
    Config-consuming work is delegated with that mapping rather than with
    individual arguments, because a ``--workers`` child process rebuilds the
    server from a serialized copy of it and never parses the CLI itself.
    """
    configure_logging_from_params(ctx.params)

    # stateless_http arrives as a ParsedStatelessHttp from its Click callback:
    # the resolved value plus any input that could not be read. Both are handed
    # to resolve_worker_settings, which reports the rejected text now that log
    # handlers exist and turns the "None means decide for me" default into a
    # concrete bool.
    stateless_http_requested = stateless_http.value is not None

    try:
        workers, stateless_http = resolve_worker_settings(
            workers=workers,
            stateless_http=stateless_http.value,
            transport=transport,
            invalid_stateless_http=stateless_http.invalid_token,
        )
    except WorkerConfigError as e:
        raise click.UsageError(str(e)) from e

    # Write the resolved topology back so there is one source of truth for it:
    # the server built here, the config handed to worker processes, and the
    # support diagnostics all read these two keys.
    ctx.params["workers"] = workers
    ctx.params["stateless_http"] = stateless_http

    if stateless_http_requested and transport not in NETWORK_TRANSPORTS:
        logger.warning(
            "--stateless-http/CB_MCP_STATELESS_HTTP is only honored for network "
            "transports; ignoring it for transport=%s.",
            transport,
        )

    if workers > 1:
        # Resolve OAuth purely to validate it: a bad OAuth configuration should
        # surface as one usage error from the supervisor, not as N workers that
        # spawn, raise, and get restarted.
        try:
            resolve_oauth_from_params(ctx.params)
        except OAuthConfigError as e:
            raise click.UsageError(str(e)) from e

        # One startup event for the deployment, sent by the supervisor; workers
        # are told to stay quiet so N processes don't look like N installs.
        send_install_ping(transport)
        ctx.params["send_startup_ping"] = False
        run_workers(ctx.params)
        return

    try:
        mcp = build_mcp_server(ctx.params)
    except OAuthConfigError as e:
        raise click.UsageError(str(e)) from e

    # Map user-friendly transport names to SDK transport names
    sdk_transport = NETWORK_TRANSPORTS_SDK_MAPPING.get(transport, transport)

    run_kwargs: dict[str, Any] = {}
    if transport in NETWORK_TRANSPORTS:
        run_kwargs = {
            "host": host,
            "port": port,
            "stateless_http": stateless_http,
        }
    mcp.run(transport=sdk_transport, show_banner=False, **run_kwargs)  # type: ignore


if __name__ == "__main__":
    main()
