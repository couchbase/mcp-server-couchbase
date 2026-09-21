import logging

from couchbase_operational_insights.cluster import Cluster
from couchbase_operational_insights.credential import Credential

from ...servers.operational_insights.constants import (
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)
from .sdk_logging import quiesce_new_root_handlers

logger = logging.getLogger(f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.utils.connection")


def connect_to_operational_insights_cluster(
    connection_string: str | None, username: str | None, password: str | None
) -> Cluster:
    """Connect to an Operational Insights cluster and return the cluster object.

    ``connection_string`` is an HTTP(S) URL — e.g. ``http://localhost:8095``
    for a local Operational Insights server, or ``https://<host>:18095`` for
    Capella — not a ``couchbase://`` connection string.

    Unlike the operational server's ``connect_to_couchbase_cluster``, the CLI
    options for this server are optional (so the server can start in "lazy",
    no-cluster mode for tool discovery), so the values are validated here
    rather than by Click's ``required=True``. If the connection fails, it
    will raise an exception.
    """
    missing = [
        name
        for name, value in (
            ("connection_string", connection_string),
            ("username", username),
            ("password", password),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Missing Operational Insights credentials: "
            f"{', '.join(missing)}. Set them via --connection-string/"
            "--username/--password or CB_OI_CONNECTION_STRING/"
            "CB_OI_USERNAME/CB_OI_PASSWORD. connection_string is an HTTP(S) "
            "URL (e.g. http://localhost:8095), not a couchbase:// string."
        )

    try:
        logger.info("Connecting to Operational Insights cluster...")
        credential = Credential.from_username_and_password(username, password)
        # The SDK's own import-time logging setup (protocol/__init__.py's
        # configure_logger()) turns out to actually fire here, on the first
        # real connection, not at module-import time — this call is what
        # triggers `couchbase_operational_insights.protocol` to be imported.
        # Scoped tightly around just this call (not the whole function) so
        # only a handler that appears during this exact call is treated as
        # the SDK's, regardless of whether the connection succeeds.
        with quiesce_new_root_handlers():
            cluster = Cluster.create_instance(connection_string, credential)
        logger.info("Successfully connected to Operational Insights cluster")
        return cluster
    except Exception as e:
        logger.error(
            "Failed to connect to Operational Insights cluster: %s. Verify the "
            "endpoint is an HTTP(S) URL (not a couchbase:// string) and the "
            "credentials are an Operational Insights user.",
            e,
            exc_info=True,
        )
        raise
