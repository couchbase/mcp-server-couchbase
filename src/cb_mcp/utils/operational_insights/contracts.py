"""The provider contract for the Operational Insights server.

Lives here rather than in ``cb_mcp.core.contracts`` for the same reason the
helpers around it do: it names ``couchbase_operational_insights`` types, and
``core`` must stay free of both SDKs (see that module's docstring). ``core``
owns only the service-agnostic half, ``ProviderLifecycle``.

The practical gap this closes: ``get_oi_handle_registry`` reaches
``provider.handle_registry`` on a value the shared layer types as
``ProviderLifecycle``, which has no such member. Declaring the attribute in
a protocol makes that reach checkable instead of an ``AttributeError``
waiting for the first provider that forgets it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fastmcp import Context

from ...core.contracts import ProviderLifecycle
from .handle_registry import HandleRegistry

if TYPE_CHECKING:
    from couchbase_operational_insights.cluster import Cluster


class OperationalInsightsProvider(ProviderLifecycle, Protocol):
    """Resolves an Operational Insights cluster, and owns its handle registry.

    Two members beyond ``ProviderLifecycle``:

    ``get_cluster``
        Same role as ``ClusterProvider.get_cluster``, different SDK — this
        ``Cluster`` is ``couchbase_operational_insights``', unrelated to the
        Couchbase one despite the shared name.

    ``handle_registry``
        An *attribute*, not a method, and the reason this server needs its
        own protocol rather than a parameterized ``ClusterProvider``: async
        query handles outlive a single tool call but cannot be serialized to
        the client, so they live on the provider (same lifetime as the
        connection). See ``handle_registry.py`` for why it is not on the
        shared ``AppContext``.
    """

    handle_registry: HandleRegistry

    def get_cluster(self, ctx: Context) -> Cluster:
        """Return (or begin returning) a cluster for this request."""
        ...
