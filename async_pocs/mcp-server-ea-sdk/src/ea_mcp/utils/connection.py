"""Enterprise Analytics cluster connection helpers."""

import logging

from couchbase_analytics.cluster import Cluster
from couchbase_analytics.credential import Credential

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.connection")


def connect_to_ea_cluster(endpoint: str, username: str, password: str) -> Cluster:
    """Create a blocking Enterprise Analytics ``Cluster`` instance.

    Args:
        endpoint: The Analytics query service endpoint, e.g.
            ``http://localhost:9095`` (or ``https://...`` when TLS is enabled).
        username: EA username.
        password: EA password.

    Returns:
        A connected :class:`couchbase_analytics.cluster.Cluster`.
    """
    if not endpoint or not username or not password:
        raise ValueError(
            "Enterprise Analytics endpoint, username, and password are all "
            "required. Provide --endpoint/--username/--password or the "
            "EA_ENDPOINT/EA_USERNAME/EA_PASSWORD environment variables."
        )
    try:
        credential = Credential.from_username_and_password(username, password)
        cluster = Cluster.create_instance(endpoint, credential)
        logger.info("Connected to Enterprise Analytics at %s", endpoint)
        return cluster
    except Exception as e:
        logger.error("Failed to connect to Enterprise Analytics: %s", e)
        raise
