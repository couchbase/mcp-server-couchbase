"""Lifespan-scoped context for the Enterprise Analytics MCP server."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from couchbase_analytics.cluster import Cluster
from fastmcp import Context

from ..core.contracts import EAClusterProvider
from .handle_registry import HandleRegistry


@dataclass
class AppContext:
    """Lifespan-scoped context shared across tool calls.

    Attributes:
        cluster_provider: Resolves the EA cluster.
        handle_registry: Server-side store of live async query handles keyed by
            opaque token. Created once per server process — this is what lets a
            handle produced by ``run_query_async`` be found again by later tool
            calls (see handle_registry.py for the process-boundary caveats).
        settings: Snapshot of CLI/env-resolved configuration.
    """

    cluster_provider: EAClusterProvider | None = None
    handle_registry: HandleRegistry = field(default_factory=HandleRegistry)
    settings: Mapping[str, Any] = field(default_factory=dict)


def _lifespan(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context  # type: ignore[return-value]


def get_cluster_connection(ctx: Context) -> Cluster:
    """Return the EA cluster for this request via the provider."""
    app = _lifespan(ctx)
    if app.cluster_provider is None:
        raise RuntimeError(
            "Cluster provider not initialized. The lifespan must populate "
            "AppContext.cluster_provider before tools run."
        )
    return app.cluster_provider.get_cluster(ctx)


def get_handle_registry(ctx: Context) -> HandleRegistry:
    """Return the async-query handle registry for this server."""
    return _lifespan(ctx).handle_registry
