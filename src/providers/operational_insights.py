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
from cb_mcp.utils.operational_insights.handle_registry import HandleRegistry

logger = logging.getLogger(
    f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.providers.operational_insights"
)


class OperationalInsightsClusterProvider:
    """Cluster provider for the standalone host, Operational Insights server.

    Same shape as ``OperationalClusterProvider``: one cluster for the life of the
    server, created lazily on first request under a ``threading.Lock``
    (tool handlers run in FastMCP's thread pool, so concurrent first calls
    coalesce on a threading — not asyncio — lock). Satisfies
    ``cb_mcp.utils.operational_insights.contracts.OperationalInsightsProvider``
    structurally — that protocol's ``ProviderLifecycle`` half is what the
    shared machinery calls, and its other two members (``get_cluster``
    returning an OI ``Cluster``, and ``handle_registry``) are what this
    server's own tools reach for.

    Two differences from ``OperationalClusterProvider`` make this a separate
    class rather than a parameterization of it: teardown (``shutdown()``,
    not ``close()``) and the handle registry.
    """

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self._settings = settings
        self._cluster: Cluster | None = None
        self._lock = threading.Lock()
        # One registry per provider instance — same lifetime as the cluster
        # connection. See handle_registry.py for why it lives here rather
        # than on the shared AppContext.
        self.handle_registry = HandleRegistry()

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
            self._settings.get("ca_cert_path"),  # type: ignore[arg-type]
            self._settings.get("client_cert_path"),  # type: ignore[arg-type]
            self._settings.get("client_key_path"),  # type: ignore[arg-type]
            self._settings.get("client_cert_password"),  # type: ignore[arg-type]
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
            "ca_cert_path_configured": bool(s.get("ca_cert_path")),
            "client_cert_path_configured": bool(s.get("client_cert_path")),
            "client_key_path_configured": bool(s.get("client_key_path")),
            "client_cert_password_configured": bool(s.get("client_cert_password")),
        }

    def is_connected(
        self, ctx: Context
    ) -> bool:  # ctx unused; one cluster shared across callers
        """True if a cluster is currently open for this caller.

        Reflects cache state at the moment of the call. Does not wait for
        in-flight connection attempts to settle.
        """
        return self._cluster is not None
