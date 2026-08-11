import logging
import threading
from collections.abc import Mapping
from typing import Any

from couchbase.cluster import Cluster
from couchbase_analytics.cluster import Cluster as AnalyticsCluster
from fastmcp import Context

from cb_mcp.utils.connection import (
    connect_to_analytics_cluster,
    connect_to_couchbase_cluster,
)
from cb_mcp.utils.constants import CONNECTION_MODE_ANALYTICS, MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.providers.static")


class StaticClusterProvider:
    """Cluster provider for the standalone host

    Opens a single cluster for the life of the server using the
    connection string, credentials, and cert paths supplied via CLI
    flags or environment variables. The cluster is created lazily on
    first request so that ``--help`` and tool discovery don't require a
    live Couchbase.

    ``settings["connection_mode"]`` selects which SDK/cluster type is
    opened: the operational ``couchbase.cluster.Cluster`` (default), or the
    unrelated ``couchbase_analytics.cluster.Cluster`` when set to
    "analytics". A single provider instance only ever holds one of the two,
    never both.

    Tool handlers run in FastMCP's thread pool (anyio ``to_thread``),
    so concurrent first calls coalesce on a ``threading.Lock`` rather
    than an ``asyncio.Lock``.
    """

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self._settings = settings
        self._cluster: Cluster | AnalyticsCluster | None = None
        self._lock = threading.Lock()

    def get_cluster(
        self, ctx: Context
    ) -> Cluster | AnalyticsCluster:  # ctx unused; settings come from init
        """Return the shared cluster, connecting on the first call."""
        if self._cluster is not None:
            return self._cluster
        with self._lock:
            if self._cluster is None:
                self._cluster = self._connect()
        return self._cluster

    def _connect(self) -> Cluster | AnalyticsCluster:
        """Open a new cluster connection from the init-time settings."""
        if self._settings.get("connection_mode") == CONNECTION_MODE_ANALYTICS:
            try:
                return connect_to_analytics_cluster(
                    self._settings.get("connection_string"),  # type: ignore[arg-type]
                    self._settings.get("username"),  # type: ignore[arg-type]
                    self._settings.get("password"),  # type: ignore[arg-type]
                    self._settings.get("ca_cert_path"),
                )
            except Exception as e:
                logger.error(
                    "Failed to connect to Enterprise Analytics: %s\n"
                    "Verify the connection string is an EA endpoint "
                    "(http(s)://host[:18095]) and username/password are correct.",
                    e,
                )
                raise

        try:
            return connect_to_couchbase_cluster(
                self._settings.get("connection_string"),  # type: ignore[arg-type]
                self._settings.get("username"),  # type: ignore[arg-type]
                self._settings.get("password"),  # type: ignore[arg-type]
                self._settings.get("ca_cert_path"),
                self._settings.get("client_cert_path"),
                self._settings.get("client_key_path"),
            )
        except Exception as e:
            logger.error(
                "Failed to connect to Couchbase: %s\n"
                "Verify connection string, and either:\n"
                "- Username/password are correct, or\n"
                "- Client certificate and key exist and match server mapping.\n"
                "If using self-signed or custom CA, set CB_CA_CERT_PATH to the CA file.",
                e,
            )
            raise

    def close(self) -> None:
        """Close the cluster connection and reset internal state."""
        cluster = self._cluster
        if cluster is not None:
            if isinstance(cluster, AnalyticsCluster):
                cluster.shutdown()
            else:
                cluster.close()
            self._cluster = None

    def get_configuration(
        self, ctx: Context
    ) -> Mapping[str, Any]:  # ctx unused; settings come from init
        """Return credential-related configuration. Never includes secrets."""
        # connection_mode is a server-level setting (like read_only_mode),
        # surfaced by get_server_configuration_status directly from
        # AppContext — not duplicated here, since contracts.ClusterProvider
        # forbids provider configs from reusing server-level key names.
        s = self._settings
        return {
            "connection_string": s.get("connection_string", "Not set"),
            "username": s.get("username", "Not set"),
            "password_configured": bool(s.get("password")),
            "ca_cert_path_configured": bool(s.get("ca_cert_path")),
            "client_cert_path_configured": bool(s.get("client_cert_path")),
            "client_key_path_configured": bool(s.get("client_key_path")),
        }

    def is_connected(
        self, ctx: Context
    ) -> bool:  # ctx unused; one cluster shared across callers
        """True if a cluster is currently open for this caller.
        Reflects cache state at the moment of the call. Does not wait for
        in-flight connection attempts to settle — concurrent tools that are
        mid-connect will not yet be reflected here. Callers that want a
        definitive answer should connect explicitly via test_cluster_connection.
        """
        return self._cluster is not None
