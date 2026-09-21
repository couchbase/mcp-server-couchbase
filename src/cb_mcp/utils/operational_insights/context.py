from couchbase_operational_insights.cluster import Cluster
from fastmcp import Context

from ..context import get_cluster_provider


def get_oi_cluster(ctx: Context) -> Cluster:
    """Return the Operational Insights cluster for this request via the provider.

    Same body as ``cb_mcp.utils.context.get_cluster_connection``, with the
    Operational Insights ``Cluster`` type instead of the operational one —
    kept as a sibling function rather than changing the shared one so that
    ``cb_mcp.utils.context`` and ``cb_mcp.core.contracts`` stay untouched.
    """
    provider = get_cluster_provider(ctx)
    if provider is None:
        raise RuntimeError(
            "Cluster provider not initialized. "
            "The lifespan must populate AppContext.cluster_provider before tools run."
        )
    return provider.get_cluster(ctx)
