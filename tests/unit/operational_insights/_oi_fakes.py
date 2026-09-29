"""Shared fake-context helper for Operational Insights tool unit tests.

Builds a real ``cb_mcp.utils.context.AppContext`` (not a bare
``SimpleNamespace``) wrapping a mock ``ClusterProvider``, so each tool test
exercises the actual ``get_cluster_provider``/``get_oi_cluster`` indirection
instead of stubbing it out — and so a rename of ``AppContext``'s fields
breaks these tests, rather than silently testing nothing.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from cb_mcp.utils.context import AppContext
from cb_mcp.utils.operational_insights.handle_registry import HandleRegistry


def make_oi_ctx(*, read_only_mode: bool = True) -> tuple[SimpleNamespace, MagicMock]:
    """Build a fake ``Context`` plus its underlying cluster mock.

    Returns ``(ctx, cluster)`` so each test can program
    ``cluster.execute_query(...).get_all_rows()`` directly. The provider's
    ``handle_registry`` is a real ``HandleRegistry`` (not a further mock) —
    accessing it on a bare ``MagicMock`` would otherwise auto-vend a fake
    attribute, and the async query tools need real token minting/eviction.
    """
    cluster = MagicMock()
    provider = MagicMock()
    provider.get_cluster.return_value = cluster
    provider.handle_registry = HandleRegistry()
    app_context = AppContext(
        cluster_provider=provider,
        read_only_mode=read_only_mode,
        server_id="operational-insights",
    )
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app_context))
    return ctx, cluster
