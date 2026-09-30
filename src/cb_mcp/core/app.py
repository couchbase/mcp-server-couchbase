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
    LOGGER_NAMESPACE,
    NETWORK_TRANSPORTS,
    NETWORK_TRANSPORTS_SDK_MAPPING,
)
from ..utils.context import AppContext
from ..utils.environment import log_environment_info
from ..utils.telemetry import send_install_ping
from .contracts import ProviderLifecycle
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
) -> None:
    """Initialise auditing and write the two startup records.

    Emits nothing at all when the server declares no audit package, or when
    auditing is configured off or its sink could not be opened.
    """
    audit = init_audit(audit_config)
    if not audit.active:
        return

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
        audit_config=audit_config.as_dict(),
    )


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

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        """Build the lifespan AppContext from host-resolved configuration."""
        transport = settings.get("transport")
        # Audit is initialised first so the server-started record is the first
        # line in the file and every later record shares its static context.
        if audits:
            _start_audit(
                spec,
                audit_config,  # type: ignore[arg-type]
                transport=str(transport),
                oauth_enabled=bool(settings.get("oauth_enabled", False)),
                read_only_mode=read_only_mode,
                registered_tool_names=[tool.__name__ for tool in tools],
                settings=settings,
            )
        # Name the server: both servers log into the same hierarchy, so in an
        # aggregated stream these lines are otherwise indistinguishable. The
        # wire-visible name is a static fact and lives in the env-info record,
        # not repeated on every startup line.
        logger.info(
            f"MCP server '{spec.id}' initialized in lazy mode for tool "
            f"discovery. Modes: (read_only_mode={read_only_mode})"
        )
        # Diagnostic snapshot for customer support. Filtered at INFO; visible
        # whenever the user runs with --log-level DEBUG.
        log_environment_info(transport, settings, spec)
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
                settings=settings,
                read_only_mode=read_only_mode,
                logging_config=logging_config,
                audit_config=(
                    audit_config.as_dict() if audit_config is not None else None
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


def run_app(
    mcp: FastMCP,
    *,
    transport: str,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Run ``mcp`` on ``transport``, translating our transport names to the SDK's.

    ``host``/``port`` are forwarded only for network transports; passing them
    for stdio is an error in the SDK rather than a no-op.
    """
    sdk_transport = NETWORK_TRANSPORTS_SDK_MAPPING.get(transport, transport)
    run_kwargs: dict[str, Any] = {}
    if transport in NETWORK_TRANSPORTS:
        run_kwargs = {"host": host, "port": port}
    mcp.run(transport=sdk_transport, show_banner=False, **run_kwargs)  # type: ignore[arg-type]
