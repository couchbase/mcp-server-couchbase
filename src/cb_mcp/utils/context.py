"""Lifespan-scoped context and the server-agnostic accessors over it.

Everything here works for any server: the provider, the settings snapshot,
the server identity, the logging snapshot. Anything that resolves a *cluster*
lives with its own server's helpers instead —
``cb_mcp.utils.operational.context.get_cluster_connection`` and
``cb_mcp.utils.operational_insights.context.get_oi_cluster`` — since each
names a different SDK's type.

That split is why this module no longer imports either SDK, even under
``TYPE_CHECKING``: ``cb_mcp.utils.__init__`` re-exports from here, so a
runtime import here reached every process that touched ``cb_mcp.utils``,
including the Operational Insights server's. See
``tests/unit/test_sdk_isolation.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Context

from ..core.contracts import ProviderLifecycle


@dataclass
class AppContext:
    """Lifespan-scoped context for the MCP server.

    Attributes:
        cluster_provider: The host's provider implementation. Typed as
            ``ProviderLifecycle`` — the service-agnostic half of the contract
            — because this field is shared by every server and nothing
            reached through it here is service-specific. A tool that needs
            the cluster itself goes through its own server's accessor
            (``get_cluster_connection`` here,
            ``cb_mcp.utils.operational_insights.context.get_oi_cluster``
            there), which narrows to that service's provider protocol.
            The standalone MCP server populates this with ``OperationalClusterProvider``
            during lifespan startup; other implementations supply their own.
        settings: Snapshot of CLI/environment-resolved configuration
            captured once at lifespan startup. Tools should read values
            from here via :func:`cb_mcp.utils.config.get_settings` rather than
            reaching for a module global.
        read_only_mode: When True, all write operations (KV, Query, and index
            management) are disabled and KV and index write tools are not loaded.
        logging_config: Optional snapshot of the active logging configuration,
            populated by the server entrypoint after configuring its loggers.
        server_id: Which server this process is running (e.g. "operational").
            An identity, not configuration — nobody sets it, so it is kept off
            ``settings``, which holds operator-resolved values only. Populated
            from the spec by ``cb_mcp.core.app.build_app``; a host building this
            context itself may leave it unset.
    """

    cluster_provider: ProviderLifecycle | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    read_only_mode: bool = True
    logging_config: Mapping[str, Any] | None = None
    server_id: str | None = None
    server_name: str | None = None


def get_cluster_provider(ctx: Context) -> ProviderLifecycle | None:
    """Return this request's provider, as the service-agnostic contract.

    Callers needing a service-specific member (``get_cluster``, or the
    Operational Insights server's ``handle_registry``) narrow via their own
    server's accessor rather than widening this return type — that is what
    keeps this module from naming either SDK.
    """
    return ctx.request_context.lifespan_context.cluster_provider  # type: ignore[no-any-return]


def get_server_id(ctx: Context) -> str | None:
    """Return which server is running, or None if the host did not set it.

    ``getattr`` with a default, like :func:`get_logging_config`: an embedding
    host may supply a lifespan-context type that predates this field.
    """
    return getattr(ctx.request_context.lifespan_context, "server_id", None)


def get_server_name(ctx: Context) -> str | None:
    """Return the wire-visible server name, or None if the host did not set it."""
    return getattr(ctx.request_context.lifespan_context, "server_name", None)


def get_logging_config(ctx: Context) -> Mapping[str, Any] | None:
    """Return the logging-config snapshot attached to the lifespan context.

    Returns ``None`` when the server entrypoint doesn't populate the
    field (e.g., implementations that don't use ``configure_logging`` from
    :mod:`cb_mcp.utils.logging`) — including host servers whose lifespan
    context type doesn't carry a ``logging_config`` attribute at all.
    """
    return getattr(ctx.request_context.lifespan_context, "logging_config", None)  # type: ignore
