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
        read_only_mode: When True, all write operations (KV and Query) are
            disabled and KV write tools are not loaded.
        logging_config: Optional snapshot of the active logging configuration,
            populated by the server entrypoint after configuring its loggers.
    """

    cluster_provider: ClusterProvider | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    read_only_mode: bool = True
    logging_config: Mapping[str, Any] | None = None


def get_cluster_provider(ctx: Context):
    """Return the ClusterProvider for this request."""
    return ctx.request_context.lifespan_context.cluster_provider  # type: ignore


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
