"""Enterprise Analytics cluster connection helpers."""

import logging
from datetime import timedelta

from couchbase_analytics.cluster import Cluster
from couchbase_analytics.credential import Credential
from couchbase_analytics.options import ClusterOptions, TimeoutOptions

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.connection")


def connect_to_ea_cluster(
    endpoint: str,
    username: str,
    password: str,
    query_timeout_seconds: float | None = None,
) -> Cluster:
    """Create a blocking Enterprise Analytics ``Cluster`` instance.

    Args:
        endpoint: The Analytics query service endpoint, e.g.
            ``http://localhost:9095`` (or ``https://...`` when TLS is enabled).
        username: EA username.
        password: EA password.
        query_timeout_seconds: Whole-request deadline for streaming queries.
            This is not an idle timeout -- it keeps running while a cursor sits
            paused between tool calls, so a stream consumed more slowly than
            this will expire mid-iteration.

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
        options = None
        if query_timeout_seconds is not None:
            options = ClusterOptions(
                timeout_options=TimeoutOptions(
                    query_timeout=timedelta(seconds=query_timeout_seconds)
                )
            )
        cluster = Cluster.create_instance(endpoint, credential, options)
        logger.info(
            "Connected to Enterprise Analytics at %s (query_timeout=%ss)",
            endpoint,
            query_timeout_seconds,
        )
        return cluster
    except Exception as e:
        logger.error("Failed to connect to Enterprise Analytics: %s", e)
        raise
