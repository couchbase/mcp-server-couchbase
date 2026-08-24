"""Static cluster provider for the standalone EA MCP server."""

import logging
import threading
from collections.abc import Mapping
from typing import Any

from couchbase_analytics.cluster import Cluster
from fastmcp import Context

from ea_mcp.utils.connection import connect_to_ea_cluster
from ea_mcp.utils.constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.providers.static")


class StaticEAClusterProvider:
    """Opens a single EA cluster for the life of the server.

    Uses the endpoint/credentials supplied via CLI flags or environment
    variables. The cluster is created lazily on first request so that ``--help``
    and tool discovery don't require a live Enterprise Analytics.

    Tool handlers run in FastMCP's thread pool, so concurrent first calls
    coalesce on a ``threading.Lock``.
    """

    def __init__(self, settings: Mapping[str, Any]) -> None:
        self._settings = settings
        self._cluster: Cluster | None = None
        self._lock = threading.Lock()

    def get_cluster(self, ctx: Context) -> Cluster:  # ctx unused; static config
        if self._cluster is not None:
            return self._cluster
        with self._lock:
            if self._cluster is None:
                self._cluster = connect_to_ea_cluster(
                    self._settings.get("endpoint"),  # type: ignore[arg-type]
                    self._settings.get("username"),  # type: ignore[arg-type]
                    self._settings.get("password"),  # type: ignore[arg-type]
                )
        return self._cluster

    def close(self) -> None:
        cluster = self._cluster
        if cluster is not None:
            try:
                cluster.shutdown()
            except Exception as e:  # noqa: BLE001
                logger.warning("Error shutting down EA cluster: %s", e)
            self._cluster = None
