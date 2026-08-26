"""Host-agnostic contract for resolving an EA cluster."""

from typing import Protocol, runtime_checkable

from couchbase_analytics.cluster import Cluster
from fastmcp import Context


@runtime_checkable
class EAClusterProvider(Protocol):
    """Resolves an Enterprise Analytics cluster for a request.

    Implementations decide how credentials are sourced and how the cluster is
    cached (one per server, one per principal, etc.).
    """

    def get_cluster(self, ctx: Context) -> Cluster:
        """Return (or begin returning) a cluster for this request."""
        ...

    def close(self) -> None:
        """Release the cluster held by this provider."""
        ...
