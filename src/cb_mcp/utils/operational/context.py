"""Accessors that narrow the shared provider to this server's contract.

``cb_mcp.utils.context.get_cluster_provider`` returns a ``ProviderLifecycle``
— only what the shared machinery calls. This module narrows that to
``ClusterProvider`` so the operational server's tools get a Couchbase
``Cluster``.

Lives here rather than in ``cb_mcp.utils.context`` (where it was, back when
"shared" and "Couchbase" meant the same thing) for two reasons: it is the
exact mirror of
``cb_mcp.utils.operational_insights.context.get_oi_cluster``, and it is the
only thing that made the shared context module name an SDK at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastmcp import Context

from ...core.contracts import ClusterProvider
from ..context import get_cluster_provider

if TYPE_CHECKING:
    from couchbase.cluster import Cluster

_PROVIDER_MISSING = (
    "Cluster provider not initialized. "
    "The lifespan must populate AppContext.cluster_provider before tools run."
)


def get_cluster_connection(ctx: Context) -> Cluster:
    """Return the Couchbase cluster for this request via the provider.

    ``cast`` rather than an ``isinstance`` check: these protocols are not
    ``runtime_checkable`` (see ``core/contracts.py`` for why that would be
    misleading), and the pairing is guaranteed structurally anyway — the
    subcommand that built this process passed this server's spec *and* this
    server's provider factory to ``build_app`` together.
    """
    provider = get_cluster_provider(ctx)
    if provider is None:
        raise RuntimeError(_PROVIDER_MISSING)
    return cast(ClusterProvider, provider).get_cluster(ctx)
