"""Connection handling for the Enterprise Analytics prototype MCP server.

Deliberately minimal compared to the parent ``cb_mcp`` package's
``ClusterProvider``/``StaticClusterProvider`` abstraction: a single ``Cluster``
is connected once at server startup and stashed on ``AppContext``, with no
lazy-connect-on-first-call, no lock, and no ``is_connected()``/
``get_configuration()`` status tooling.
"""

import logging
from dataclasses import dataclass

from couchbase_analytics.cluster import Cluster
from couchbase_analytics.credential import Credential
from fastmcp import Context

logger = logging.getLogger("ea-mcp-server.connection")


@dataclass
class AppContext:
    """Lifespan-scoped context for the MCP server: just the connected cluster."""

    cluster: Cluster


def connect_to_analytics_cluster(
    connection_string: str, username: str, password: str
) -> Cluster:
    """Connect to an Enterprise Analytics cluster and return the cluster object.

    If the connection fails, it will raise an exception.
    """
    try:
        logger.info("Connecting to Enterprise Analytics cluster...")
        credential = Credential.from_username_and_password(username, password)
        cluster = Cluster.create_instance(connection_string, credential)
        logger.info("Successfully connected to Enterprise Analytics cluster")
        return cluster
    except Exception as e:
        logger.error(
            f"Failed to connect to Enterprise Analytics cluster: {e}", exc_info=True
        )
        raise


def get_cluster_connection(ctx: Context) -> Cluster:
    """Return the Enterprise Analytics cluster for this request."""
    return ctx.request_context.lifespan_context.cluster  # type: ignore
