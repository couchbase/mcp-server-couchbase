"""Connection handling for the Enterprise Analytics prototype MCP server.

Deliberately minimal compared to the parent ``cb_mcp`` package's
``ClusterProvider``/``StaticClusterProvider`` abstraction: a single ``Cluster``
is connected once at server startup and stashed on ``AppContext``, with no
lazy-connect-on-first-call, no lock, and no ``is_connected()``/
``get_configuration()`` status tooling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from couchbase_analytics.cluster import Cluster
from couchbase_analytics.credential import Credential
from fastmcp import Context

from .handle_registry import HandleRegistry
from .result_config import ResultConfig
from .result_store import ResultStore

logger = logging.getLogger("ea-mcp-server.connection")


@dataclass
class AppContext:
    """Lifespan-scoped context for the MCP server.

    Holds the connected cluster, the async-query handle registry, and the
    large-result settings and store. The registry is created once per server
    process here (rather than as a module global) so its lifetime is tied to
    the lifespan, and tests can build an isolated one per case.

    ``result_store`` is None when large-result saving is disabled, which is
    the default: with nothing to save there is no reason to create a storage
    directory.
    """

    cluster: Cluster
    handle_registry: HandleRegistry = field(default_factory=HandleRegistry)
    result_config: ResultConfig = field(default_factory=ResultConfig)
    result_store: ResultStore | None = None


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


def get_handle_registry(ctx: Context) -> HandleRegistry:
    """Return the async query handle registry for this server process."""
    return ctx.request_context.lifespan_context.handle_registry  # type: ignore


def get_result_config(ctx: Context) -> ResultConfig:
    """Return the large-result settings for this server process."""
    return ctx.request_context.lifespan_context.result_config  # type: ignore


def get_result_store(ctx: Context) -> ResultStore | None:
    """Return the saved-result store, or None when saving is disabled."""
    return ctx.request_context.lifespan_context.result_store  # type: ignore
