"""Lifespan-scoped context for the Enterprise Analytics streaming MCP server."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from couchbase_analytics.cluster import Cluster
from fastmcp import Context

from ..core.contracts import EAClusterProvider
from .cursor_registry import CursorRegistry


@dataclass
class AppContext:
    """Lifespan-scoped context shared across tool calls.

    Attributes:
        cluster_provider: Resolves the EA cluster.
        cursor_registry: Server-side store of live streaming cursors keyed by
            opaque token. Created once per server process -- this is what lets
            a cursor opened by ``stream_query_results`` be resumed by a later
            ``fetch_next_rows`` call (see cursor_registry.py for the
            process-boundary caveats).
        settings: Snapshot of CLI/env-resolved configuration.
    """

    cluster_provider: EAClusterProvider | None = None
    cursor_registry: CursorRegistry | None = None
    settings: Mapping[str, Any] = None  # type: ignore[assignment]


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


def get_cursor_registry(ctx: Context) -> CursorRegistry:
    """Return the streaming cursor registry for this server."""
    app = _lifespan(ctx)
    if app.cursor_registry is None:
        raise RuntimeError(
            "Cursor registry not initialized. The lifespan must populate "
            "AppContext.cursor_registry before tools run."
        )
    return app.cursor_registry


def get_settings(ctx: Context) -> Mapping[str, Any]:
    """Return the resolved server settings."""
    return _lifespan(ctx).settings or {}
