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

from ..utils.constants import (
    LOGGER_ROOT,
    NETWORK_TRANSPORTS,
    NETWORK_TRANSPORTS_SDK_MAPPING,
)
from ..utils.context import AppContext
from ..utils.environment import log_environment_info
from ..utils.telemetry import send_install_ping
from .contracts import ClusterProvider
from .spec import ServerSpec

logger = logging.getLogger(f"{LOGGER_ROOT}.core.app")


def build_app(
    spec: ServerSpec,
    *,
    tools: Sequence[Callable],
    settings: Mapping[str, Any],
    provider_factory: Callable[[], ClusterProvider],
    auth: AuthProvider | None = None,
    read_only_mode: bool = True,
    logging_config: Mapping[str, Any] | None = None,
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

    The returned server is *not* started; the caller chooses the transport and
    calls ``run()``. See :func:`run_app` for the standard invocation.
    """

    @asynccontextmanager
    async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        """Build the lifespan AppContext from host-resolved configuration."""
        transport = settings.get("transport")
        logger.info(
            f"MCP server initialized in lazy mode for tool discovery. "
            f"Modes: (read_only_mode={read_only_mode})"
        )
        # Diagnostic snapshot for customer support. Filtered at INFO; visible
        # whenever the user runs with --log-level DEBUG.
        log_environment_info(transport, settings)
        send_install_ping(transport)
        app_context = AppContext(
            cluster_provider=provider_factory(),
            settings=settings,
            read_only_mode=read_only_mode,
            logging_config=logging_config,
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

    mcp = FastMCP(spec.fastmcp_name, lifespan=app_lifespan, auth=auth)

    logger.info(
        f"Registering {len(tools)} tool(s) with modes (read_only_mode={read_only_mode})"
    )

    # Register tools; FastMCP 3.x add_tool has no annotations kwarg, so wrap first.
    for tool in tools:
        annotations = spec.annotations.get(tool.__name__)
        tool_obj = FunctionTool.from_function(tool, annotations=annotations)
        mcp.add_tool(tool_obj)

    logger.info(f"Registered {len(tools)} tool(s)")

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
