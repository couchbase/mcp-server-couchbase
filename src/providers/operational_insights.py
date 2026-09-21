import logging
import threading
from collections.abc import Mapping
from typing import Any

from couchbase_operational_insights.cluster import Cluster
from fastmcp import Context

from cb_mcp.servers.operational_insights.constants import (
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)
from cb_mcp.utils.operational_insights.connection import (
    connect_to_operational_insights_cluster,
)

logger = logging.getLogger(
    f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.providers.operational_insights"
)


class OperationalInsightsClusterProvider:
    """Cluster provider for the standalone host, Operational Insights server.

    Same shape as ``StaticClusterProvider``: one cluster for the life of the
    server, created lazily on first request under a ``threading.Lock``
    (tool handlers run in FastMCP's thread pool, so concurrent first calls
    coalesce on a threading — not asyncio — lock). Satisfies
    ``ClusterProvider`` structurally (see ``core/contracts.py``); the one
    difference that matters is teardown, which is why this is a separate
    class rather than a parameterization of ``StaticClusterProvider``.
    """

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self._settings = settings
        self._cluster: Cluster | None = None
        self._lock = threading.Lock()

    def get_cluster(
        self, ctx: Context
    ) -> Cluster:  # ctx unused; settings come from init
        """Return the shared cluster, connecting on the first call."""
        if self._cluster is not None:
            return self._cluster
        with self._lock:
            if self._cluster is None:
                self._cluster = self._connect()
        return self._cluster

    def _connect(self) -> Cluster:
        """Open a new cluster connection from the init-time settings."""
        return connect_to_operational_insights_cluster(
            self._settings.get("connection_string"),  # type: ignore[arg-type]
            self._settings.get("username"),  # type: ignore[arg-type]
            self._settings.get("password"),  # type: ignore[arg-type]
        )

    def close(self) -> None:
        """Shut down the cluster connection and reset internal state.

        ``shutdown()``, not ``close()`` — the Operational Insights client's
        teardown verb differs from the operational Couchbase SDK's. This is
        exactly the polymorphism ``core/contracts.py`` anticipates: the
        shared lifespan calls only ``close()`` on this provider and never
        learns which verb the underlying client actually needs.
        """
        cluster = self._cluster
        if cluster is not None:
            cluster.shutdown()
            self._cluster = None

    def get_configuration(
        self, ctx: Context
    ) -> Mapping[str, Any]:  # ctx unused; settings come from init
        """Return credential-related configuration. Never includes secrets.

        Deliberately omits read_only_mode/disabled_tools/
        confirmation_required_tools — those are server-owned keys and would
        silently override the real values if returned here (see
        ClusterProvider.get_configuration's docstring).
        """
        s = self._settings
        return {
            "connection_string": s.get("connection_string", "Not set"),
            "username": s.get("username", "Not set"),
            "password_configured": bool(s.get("password")),
        }

    def is_connected(
        self, ctx: Context
    ) -> bool:  # ctx unused; one cluster shared across callers
        """True if a cluster is currently open for this caller.

        Reflects cache state at the moment of the call. Does not wait for
        in-flight connection attempts to settle.
        """
        return self._cluster is not None
