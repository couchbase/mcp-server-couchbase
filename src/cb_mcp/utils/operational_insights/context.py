"""Accessors that narrow the shared provider to this server's contract.

``cb_mcp.utils.context.get_cluster_provider`` returns a
``ProviderLifecycle`` — only what the shared machinery calls. Both functions
here do the same two things: fail loudly if the lifespan never populated the
provider, then narrow to ``OperationalInsightsProvider`` so this server's
extra members (``get_cluster``'s OI ``Cluster``, and ``handle_registry``)
are named by a contract rather than reached for and hoped about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastmcp import Context

from ..context import get_cluster_provider
from .contracts import OperationalInsightsProvider

if TYPE_CHECKING:
    from couchbase_operational_insights.cluster import Cluster

    from .handle_registry import QueryResultsRegistry

_PROVIDER_MISSING = (
    "Cluster provider not initialized. "
    "The lifespan must populate AppContext.cluster_provider before tools run."
)


def _provider(ctx: Context) -> OperationalInsightsProvider:
    """The request's provider, narrowed to this server's contract.

    ``cast`` rather than an ``isinstance`` check: the protocols here are not
    ``runtime_checkable`` (see ``core/contracts.py`` for why that would be
    misleading), and the pairing is guaranteed structurally anyway — the
    subcommand that built this process passed this server's spec *and* this
    server's provider factory to ``build_app`` together.
    """
    provider = get_cluster_provider(ctx)
    if provider is None:
        raise RuntimeError(_PROVIDER_MISSING)
    return cast(OperationalInsightsProvider, provider)


def get_oi_cluster(ctx: Context) -> Cluster:
    """Return the Operational Insights cluster for this request via the provider.

    Same role as ``cb_mcp.utils.operational.context.get_cluster_connection``,
    its exact mirror on the other server, with the
    Operational Insights ``Cluster`` — an unrelated SDK's type that happens
    to share the name.
    """
    return _provider(ctx).get_cluster(ctx)


def get_oi_handle_registry(ctx: Context) -> QueryResultsRegistry:
    """Return the Operational Insights async-query handle registry.

    The registry lives on the provider, not the shared ``AppContext`` (see
    ``handle_registry.py`` for why), which is why this server needs a
    provider protocol of its own rather than a parameterized
    ``ClusterProvider``.
    """
    return _provider(ctx).handle_registry
