"""Enterprise Analytics cluster connection helper (blocking SDK)."""

import logging

from couchbase_analytics.cluster import Cluster
from couchbase_analytics.credential import Credential

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.connection")


def connect_to_ea_cluster(endpoint: str, username: str, password: str) -> Cluster:
    """Create a blocking Enterprise Analytics ``Cluster`` instance."""
    if not endpoint or not username or not password:
        raise ValueError(
            "EA endpoint, username, and password are all required "
            "(--endpoint/--username/--password or EA_ENDPOINT/EA_USERNAME/"
            "EA_PASSWORD)."
        )
    try:
        credential = Credential.from_username_and_password(username, password)
        cluster = Cluster.create_instance(endpoint, credential)
        logger.info("Connected to Enterprise Analytics at %s", endpoint)
        return cluster
    except Exception as e:
        logger.error("Failed to connect to Enterprise Analytics: %s", e)
        raise
