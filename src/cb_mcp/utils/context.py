from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from couchbase.cluster import Cluster
from fastmcp import Context

from ..core.contracts import ClusterProvider


@dataclass
class AppContext:
    """Lifespan-scoped context for the MCP server.

    Attributes:
        cluster_provider: The host's ``ClusterProvider`` implementation.
            The standalone MCP server populates this with ``StaticClusterProvider``
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

    cluster_provider: ClusterProvider | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    read_only_mode: bool = True
    logging_config: Mapping[str, Any] | None = None
    server_id: str | None = None
    server_name: str | None = None


def get_cluster_provider(ctx: Context):
    """Return the ClusterProvider for this request."""
    return ctx.request_context.lifespan_context.cluster_provider  # type: ignore


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


def get_cluster_connection(ctx: Context) -> Cluster:
    """Return the Couchbase cluster for this request via the provider."""
    provider = get_cluster_provider(ctx)
    if provider is None:
        raise RuntimeError(
            "Cluster provider not initialized. "
            "The lifespan must populate AppContext.cluster_provider before tools run."
        )
    return provider.get_cluster(ctx)
