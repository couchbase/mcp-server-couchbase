"""Assemble a runnable ``FastMCP`` application from a server specification.

This is the seam between a *host* — the standalone CLI in this repo, or an
embedding runtime such as the managed Capella service — and the shared
machinery. A host is responsible for resolving configuration (CLI flags,
environment, secret stores) and for deciding how a backing client is created;
everything downstream of that is identical for every server, and lives here.

Keeping the assembly in one place is what makes a second server cheap: it
supplies a :class:`~cb_mcp.core.spec.ServerSpec` and a provider factory, and
inherits the lifespan, diagnostics, telemetry and tool-registration behaviour
without reimplementing any of it.
"""

import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.tools import FunctionTool
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..audit import (
    AuditEvent,
    AuditMiddleware,
    ResolvedAuditConfig,
    get_audit_logger,
    init_audit,
    shutdown_audit,
    unclassified_tool_names,
    warn_on_unauthenticated_http,
)
from ..audit.record import OUTCOME_ERROR, OUTCOME_SUCCESS
from ..utils.constants import (
    HEALTH_CHECK_PATH,
    LOGGER_NAMESPACE,
    NETWORK_TRANSPORTS,
    NETWORK_TRANSPORTS_SDK_MAPPING,
    STREAMABLE_HTTP_TRANSPORT,
)
from ..utils.context import AppContext
from ..utils.environment import log_environment_info
from ..utils.telemetry import send_install_ping
from .contracts import ProviderLifecycle
from .serving import apply_thread_pool_limit
from .spec import ServerSpec

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.core.app")


def _start_audit(
    spec: ServerSpec,
    audit_config: ResolvedAuditConfig,
    *,
    transport: str,
    oauth_enabled: bool,
    read_only_mode: bool,
    registered_tool_names: list[str],
    settings: Mapping[str, Any],
) -> ResolvedAuditConfig:
    """Initialise auditing and write the two startup records.

    Emits nothing at all when the server declares no audit package, or when
    auditing is configured off or its sink could not be opened.

    Returns the configuration **actually in force**, which is not always the
    one passed in: a file sink whose path cannot be opened is dropped at this
    point. Everything that reports the audit configuration must use the value
    returned here, never the requested one.
    """
    audit = init_audit(audit_config)
    if not audit.active:
        return audit.config

    warn_on_unauthenticated_http(transport, oauth_enabled)

    unclassified = unclassified_tool_names(registered_tool_names, spec.audit_package)
    if unclassified:
        # Fail-closed classification keeps these audited, but an operator
        # should not have to discover the gap by reading the audit file.
        logger.warning(
            "Audit: %d registered tool(s) in server %r have no classification "
            "entry and will be recorded against a write event id: %s",
            len(unclassified),
            spec.id,
            unclassified,
        )

    audit.emit_event(AuditEvent.SERVER_STARTED, outcome=OUTCOME_SUCCESS)
    audit.emit_event(
        AuditEvent.SERVER_CONFIGURATION,
        outcome=OUTCOME_SUCCESS,
        server_id=spec.id,
        service_package=spec.audit_package,
        transport=transport,
        read_only_mode=read_only_mode,
        oauth_enabled=oauth_enabled,
        # The withheld and disabled tool sets are recorded here rather than as
        # per-call refusals: neither kind of tool is registered with FastMCP, so
        # a client is never told they exist and there is no invocation to
        # refuse. This record is what makes the enforced surface auditable.
        registered_tools=sorted(registered_tool_names),
        disabled_tools=sorted(settings.get("disabled_tools", ())),
        confirmation_required_tools=sorted(
            settings.get("confirmation_required_tools", ())
        ),
        # The configuration in force, not the one requested: this record is
        # permanent, and claiming a file sink that failed to open would tell an
        # auditor records were on disk when none were.
        audit_config=audit.config.as_dict(),
    )
    return audit.config


def build_app(
    spec: ServerSpec,
    *,
    tools: Sequence[Callable],
    settings: Mapping[str, Any],
    provider_factory: Callable[[], ProviderLifecycle],
    auth: AuthProvider | None = None,
    read_only_mode: bool = True,
    logging_config: Mapping[str, Any] | None = None,
    audit_config: ResolvedAuditConfig | None = None,
    thread_pool_size: int | None = None,
    send_startup_ping: bool = True,
) -> FastMCP:
    """Build the ``FastMCP`` application for ``spec``, ready to ``run()``.

    ``tools`` are the already-gated, already-wrapped callables from
    :func:`cb_mcp.tool_registration.prepare_tools_for_registration`. They are
    passed in rather than derived from ``spec.tools`` because gating depends on
    host configuration (read-only mode, disabled tools, confirmation lists)
    that this layer deliberately does not parse.

    ``provider_factory`` is called once, inside lifespan startup, rather than
    being passed as an instance: constructing a provider may open a connection,
    and nothing should connect during ``--help`` or tool discovery. It also
    gives an embedding host a hook to build a provider per principal.

    ``logging_config`` is threaded through from the host rather than read from
    this package's logging module, so a host with its own logging stack can
    populate it without adopting ours. It surfaces via
    ``get_server_configuration_status``.

    ``thread_pool_size`` caps concurrently executing tool calls in this
    process (``None`` keeps AnyIO's default); see
    :func:`cb_mcp.core.serving.apply_thread_pool_limit`. When ``settings``
    carries a ``thread_pool_size`` key, the lifespan reports the limit that
    actually took effect under it rather than the configured value.

    ``send_startup_ping`` is ``False`` for the processes of a multi-worker
    deployment, whose supervisor sends one event for all of them so N
    workers do not look like N installs.

    ``audit_config`` is threaded through from the host for the same reason as
    ``logging_config``: a host that resolves its configuration from somewhere
    other than a command line — the managed Capella runtime, for instance — can
    populate it without adopting this repo's CLI. Auditing activates only when
    the host supplies a config *and* ``spec.audit_package`` names a Tier-2
    block, so a server that has not been given an audit block cannot have its
    records booked against another server's ids.

    The returned server is *not* started; the caller chooses the transport and
    calls ``run()``. See :func:`run_app` for the standard invocation.
    """
    audits = audit_config is not None and spec.audit_package is not None

    if audit_config is not None and audit_config.enabled and spec.audit_package is None:
        # Silence here is indistinguishable from a broken audit sink. An
        # operator who set CB_MCP_AUDIT_LOG_ENABLED=true on this server would
        # otherwise find an empty audit file, or none at all, with the only
        # signal being ``active: false`` buried in the status tool.
        logger.warning(
            "Audit logging is enabled but server %r declares no audit package, "
            "so no audit records will be written. Auditing currently applies to "
            "the operational server only; the settings are accepted and "
            "reported so a shared configuration can be used unchanged.",
            spec.id,
        )

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        """Build the lifespan AppContext from host-resolved configuration."""
        transport = settings.get("transport")
        # Applied here because AnyIO's default limiter is run-scoped: it
        # exists only inside the event loop, and each worker process has its
        # own. Reported as the effective value so support sees the real cap.
        # Done before auditing starts: it writes no record, so the
        # server-started record below is still the first line in the file,
        # and that record carries the same effective value as everything else.
        effective_pool_size = apply_thread_pool_limit(thread_pool_size)
        run_settings: Mapping[str, Any] = settings
        if "thread_pool_size" in settings:
            run_settings = {**settings, "thread_pool_size": effective_pool_size}
        # Audit is initialised first so the server-started record is the first
        # line in the file and every later record shares its static context.
        # What auditing is actually doing, which differs from what was asked
        # for when a sink could not be opened. The status tool reports this.
        effective_audit_config = audit_config
        if audits:
            effective_audit_config = _start_audit(
                spec,
                audit_config,  # type: ignore[arg-type]
                transport=str(transport),
                oauth_enabled=bool(settings.get("oauth_enabled", False)),
                read_only_mode=read_only_mode,
                registered_tool_names=[tool.__name__ for tool in tools],
                settings=run_settings,
            )
        # Name the server: both servers log into the same hierarchy, so in an
        # aggregated stream these lines are otherwise indistinguishable. The
        # wire-visible name is a static fact and lives in the env-info record,
        # not repeated on every startup line.
        logger.info(
            f"MCP server '{spec.id}' initialized in lazy mode for tool "
            f"discovery. Modes: (read_only_mode={read_only_mode})"
        )
        logger.info(f"Tool-call concurrency limit: {effective_pool_size} per process")
        # Diagnostic snapshot for customer support. Filtered at INFO; visible
        # whenever the user runs with --log-level DEBUG.
        log_environment_info(transport, run_settings, spec)
        if send_startup_ping:
            send_install_ping(transport, server_id=spec.id)
        # Built inside the try, not before it: ``provider_factory`` opens a
        # connection and can fail, and auditing has already started by this
        # point — with a sink open, a writer thread running and the
        # server-started record queued. Constructing this outside the try meant
        # a provider failure skipped the cleanup below entirely, losing
        # whatever was queued and leaving the sink open for the life of the
        # process. A failed startup is exactly when the audit trail matters.
        app_context: AppContext | None = None
        failed = False
        try:
            app_context = AppContext(
                cluster_provider=provider_factory(),
                settings=run_settings,
                read_only_mode=read_only_mode,
                logging_config=logging_config,
                audit_config=(
                    effective_audit_config.as_dict()
                    if effective_audit_config is not None
                    else None
                ),
                server_id=spec.id,
                server_name=spec.fastmcp_name,
            )
            yield app_context
        except Exception as e:
            failed = True
            logger.error(f"Error in app lifespan: {e}", exc_info=True)
            raise
        finally:
            if app_context is not None and app_context.cluster_provider:
                app_context.cluster_provider.close()
            # Recorded before the sink closes so the stop record is the last
            # line written. A process killed outright leaves no stop record;
            # its absence means "not a clean shutdown", not "tampered with".
            if audits:
                active_audit = get_audit_logger()
                if active_audit.active:
                    # The catalogue describes this event as "shut down
                    # cleanly", so it must not claim success for a lifespan
                    # that exited by raising. A failed startup that recorded
                    # "server started" followed by "success" would read as a
                    # healthy run in an audit review.
                    active_audit.emit_event(
                        AuditEvent.SERVER_STOPPED,
                        outcome=OUTCOME_ERROR if failed else OUTCOME_SUCCESS,
                        reason="lifespan_error" if failed else None,
                    )
                shutdown_audit()
            logger.info("Closing MCP server")

    mcp = FastMCP(spec.fastmcp_name, lifespan=app_lifespan, auth=auth)
    _add_health_check(mcp, spec)

    # Registered before tools so it wraps every one of them. The middleware
    # short-circuits whenever the sink is inactive, so a failed sink degrades to
    # no records rather than to a half-instrumented server. ``transport``
    # decides whether an unauthenticated caller is recorded as anonymous (http)
    # or as the local process owner (stdio); ``cb_userid`` records the single
    # backing identity every caller collapses onto, which is the gap this audit
    # log exists to close.
    if audits:
        mcp.add_middleware(
            AuditMiddleware(
                transport=str(settings.get("transport")),
                cb_userid=settings.get("username"),
                service_package=spec.audit_package,  # type: ignore[arg-type]
            )
        )

    logger.info(
        f"Registering {len(tools)} tool(s) for server '{spec.id}' "
        f"with modes (read_only_mode={read_only_mode})"
    )

    # Register tools; FastMCP 3.x add_tool has no annotations kwarg, so wrap first.
    for tool in tools:
        annotations = spec.annotations.get(tool.__name__)
        tool_obj = FunctionTool.from_function(tool, annotations=annotations)
        mcp.add_tool(tool_obj)

    logger.info(f"Registered {len(tools)} tool(s) for server '{spec.id}'")

    return mcp


def _add_health_check(mcp: FastMCP, spec: ServerSpec) -> None:
    """Serve ``GET /health`` next to the MCP endpoint on network transports.

    A *liveness* check for container orchestrators and load balancers: it
    answers whenever this process can serve HTTP, and deliberately does not
    touch the backing cluster. Connections are opened lazily on first tool
    call, and a cluster outage must not make an orchestrator restart
    containers that are themselves healthy.

    Unauthenticated by design. FastMCP applies OAuth to the MCP endpoint
    only, so probes keep working when OAuth is enabled, and the response
    carries nothing beyond the server id. Present in every ``--workers``
    process too, since each builds its app through here.
    """

    @mcp.custom_route(HEALTH_CHECK_PATH, methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": spec.id})


def run_app(
    mcp: FastMCP,
    *,
    transport: str,
    host: str | None = None,
    port: int | None = None,
    stateless_http: bool = False,
) -> None:
    """Run ``mcp`` on ``transport``, translating our transport names to the SDK's.

    ``host``/``port`` are forwarded only for network transports; passing them
    for stdio is an error in the SDK rather than a no-op.

    ``stateless_http`` is always forwarded on network transports, true or
    false, so FastMCP runs exactly the mode the host resolved and validated.
    Leaving it unset would let FastMCP fall back to its own
    ``FASTMCP_STATELESS_HTTP`` setting behind the host's back; a host that
    wants to honour that setting folds it in when resolving (see
    ``cb_mcp.core.serving.resolve_serving``). SSE has no stateless mode, so
    it only ever receives ``False``.
    """
    sdk_transport = NETWORK_TRANSPORTS_SDK_MAPPING.get(transport, transport)
    run_kwargs: dict[str, Any] = {}
    if transport in NETWORK_TRANSPORTS:
        run_kwargs = {
            "host": host,
            "port": port,
            "stateless_http": stateless_http and transport == STREAMABLE_HTTP_TRANSPORT,
        }
    mcp.run(transport=sdk_transport, show_banner=False, **run_kwargs)  # type: ignore[arg-type]
