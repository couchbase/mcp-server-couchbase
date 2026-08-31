"""
Tools for server operations.

This module contains tools for getting the server status, testing the connection, and getting the buckets in the cluster, the scopes and collections in the bucket.
"""

import json
import logging
from typing import Any

from couchbase.diagnostics import ServiceType
from couchbase.options import PingOptions
from fastmcp import Context

from ..utils.config import get_settings
from ..utils.connection import connect_to_bucket
from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import (
    get_cluster_connection,
    get_cluster_provider,
    get_logging_config,
)
from .query import run_cluster_query

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.server")


def get_server_configuration_status(ctx: Context) -> dict[str, Any]:
    """Get the server status and configuration without establishing connection.
    This tool can be used to verify if the server is running and check the configuration.
    """
    settings = get_settings(ctx)
    provider = get_cluster_provider(ctx)

    provider_config = provider.get_configuration(ctx) if provider is not None else {}

    # Server-level keys are spread last so they always reflect what the server
    # actually enforces, even if a provider returns overlapping keys.
    configuration = {
        **provider_config,
        "read_only_mode": settings.get("read_only_mode", True),
        "disabled_tools": sorted(settings.get("disabled_tools", set())),
        "confirmation_required_tools": sorted(
            settings.get("confirmation_required_tools", set())
        ),
        # OAuth resource-server config (non-secret IdP coordinates). Mirrors
        # the env-info diagnostic record so the log file and this tool agree on
        # which OAuth state is exposed. ``oauth_enabled`` reflects whether OAuth
        # is actually active, not merely configured.
        "oauth_enabled": settings.get("oauth_enabled", False),
        "oauth_jwks_uri": settings.get("oauth_jwks_uri"),
        "oauth_issuer": settings.get("oauth_issuer"),
        "oauth_audience": settings.get("oauth_audience"),
        "oauth_algorithm": settings.get("oauth_algorithm"),
        "oauth_mcp_base_url": settings.get("oauth_mcp_base_url"),
        "oauth_scope_read_label": settings.get("oauth_scope_read_label"),
        "oauth_scope_write_label": settings.get("oauth_scope_write_label"),
    }

    connection_status = {
        "cluster_connected": (
            provider.is_connected(ctx) if provider is not None else False
        ),
    }

    # Surface the active logging configuration as provided by the server
    # entrypoint via the lifespan context. Falls back to ``None`` for
    # implementations that don't populate it.
    logging_status = get_logging_config(ctx)

    return {
        "server_name": MCP_SERVER_NAME,
        "status": "running",
        "configuration": configuration,
        "logging": logging_status,
        "connections": connection_status,
    }


def test_cluster_connection(
    ctx: Context, bucket_name: str | None = None
) -> dict[str, Any]:
    """Test the connection to Couchbase cluster and optionally to a bucket.
    This tool verifies the connection to the Couchbase cluster and bucket by establishing the connection if it is not already established.
    If bucket name is not provided, it will not try to connect to the bucket specified in the MCP server settings.
    Returns connection status and basic cluster information.
    """
    try:
        cluster = get_cluster_connection(ctx)
        bucket = None
        if bucket_name:
            bucket = connect_to_bucket(cluster, bucket_name)

        return {
            "status": "success",
            "cluster_connected": True,
            "bucket_connected": bucket is not None,
            "bucket_name": bucket_name,
            "message": "Successfully connected to Couchbase cluster",
        }
    except Exception as e:
        logger.error(f"Connection test failed: {e}", exc_info=True)
        return {
            "status": "error",
            "cluster_connected": False,
            "bucket_connected": False,
            "bucket_name": bucket_name,
            "error": str(e),
            "message": "Failed to connect to Couchbase cluster",
        }


def get_scopes_and_collections_in_bucket(
    ctx: Context, bucket_name: str
) -> dict[str, list[str]]:
    """Get the names of all scopes and collections in the bucket.
    Returns a dictionary with scope names as keys and lists of collection names as values.
    """
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Listing scopes and collections in bucket '{bucket_name}'")
        scopes_collections = {}
        collection_manager = bucket.collections()
        scopes = collection_manager.get_all_scopes()
        for scope in scopes:
            collection_names = [c.name for c in scope.collections]
            scopes_collections[scope.name] = collection_names
        logger.info(
            f"Found {len(scopes_collections)} scope(s) in bucket '{bucket_name}'"
        )
        return scopes_collections
    except Exception as e:
        logger.error(
            f"Error getting scopes and collections in bucket '{bucket_name}': {e}",
            exc_info=True,
        )
        raise


def get_buckets_in_cluster(ctx: Context) -> list[str]:
    """Get the names of all the accessible buckets in the cluster."""
    cluster = get_cluster_connection(ctx)
    logger.debug("Listing all buckets in cluster")
    bucket_manager = cluster.buckets()
    buckets_with_settings = bucket_manager.get_all_buckets()

    buckets = []
    for bucket in buckets_with_settings:
        buckets.append(bucket.name)

    logger.info(f"Found {len(buckets)} bucket(s) in cluster")
    return buckets


def get_scopes_in_bucket(ctx: Context, bucket_name: str) -> list[str]:
    """Get the names of all scopes in the given bucket."""
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Listing scopes in bucket '{bucket_name}'")
        scopes = bucket.collections().get_all_scopes()
        scope_names = [scope.name for scope in scopes]
        logger.info(f"Found {len(scope_names)} scope(s) in bucket '{bucket_name}'")
        return scope_names
    except Exception as e:
        logger.error(
            f"Error getting scopes in bucket '{bucket_name}': {e}", exc_info=True
        )
        raise


def get_collections_in_scope(
    ctx: Context, bucket_name: str, scope_name: str
) -> list[str]:
    """Get the names of all collections in the given scope and bucket."""

    # Get the collections in the scope using system:all_keyspaces collection
    logger.debug(f"Listing collections in {bucket_name}.{scope_name}")
    query = "SELECT DISTINCT(name) as collection_name FROM system:all_keyspaces where `bucket`=$bucket_name and `scope`=$scope_name"
    results = run_cluster_query(
        ctx, query, bucket_name=bucket_name, scope_name=scope_name
    )
    collection_names = [result["collection_name"] for result in results]
    logger.info(
        f"Found {len(collection_names)} collection(s) in {bucket_name}.{scope_name}"
    )
    return collection_names


def get_cluster_health_and_services(
    ctx: Context,
    bucket_name: str | None = None,
    service_types: list[str] | None = None,
) -> dict[str, Any]:
    """Check whether the cluster is reachable right now, and where it's broken.

    This actively pings (see caveat below) the cluster's services and reports, per service:
    - Whether it responded and how long it took (latency)
    - Which node/endpoint answered, and any error if it didn't

    Scope: cluster-level vs bucket-level ping
    - If bucket_name is omitted, this pings at the cluster level. This covers more services in
      one call, but whether the key-value (KV) service is included depends on the Couchbase
      Server version — it may be silently skipped.
    - If bucket_name is provided, this pings from the perspective of that bucket instead. This
      guarantees the KV service is covered for that bucket, but the result is scoped to that
      one bucket only — ping again per bucket_name to cover a multi-bucket cluster.

    service_types optionally restricts which services get pinged. Valid values: "key_value",
    "query", "search", "analytics", "view", "management", "eventing". Omit to ping every
    service. An unrecognized value returns an error response instead of raising.

    Caution — this is somewhat invasive: unlike a passive connection-state check, ping performs
    a live network round-trip to every targeted service. Prefer a narrow service_types filter,
    and avoid calling this in tight loops or high-frequency polling.

    Returns:
    - Cluster health status with service-level connection details and latency measurements
    """
    try:
        cluster = get_cluster_connection(ctx)

        ping_opts = (
            PingOptions(service_types=[ServiceType(s) for s in service_types])
            if service_types
            else None
        )
        ping_args = (ping_opts,) if ping_opts is not None else ()

        if bucket_name:
            # Ping services from the perspective of the bucket
            logger.debug(f"Pinging cluster services via bucket '{bucket_name}'")
            bucket = connect_to_bucket(cluster, bucket_name)
            ping_result = bucket.ping(*ping_args)
            result = ping_result.as_json()
        else:
            # Ping services from the perspective of the cluster
            logger.debug("Pinging cluster services")
            ping_result = cluster.ping(*ping_args)
            result = ping_result.as_json()

        logger.info("Retrieved cluster health and services information")
        return {
            "status": "success",
            "data": json.loads(result),
        }
    except Exception as e:
        logger.error(f"Error getting cluster health: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get cluster health and services information",
        }


def get_cluster_diagnostics_report(ctx: Context) -> dict[str, Any]:
    """Check whether the client's connections were already broken, and for how long.

    Unlike get_cluster_health_and_services (which actively pings each service right now),
    this reports the SDK's own cached connection state without performing any network I/O.
    It's cheap enough to call frequently, but it's only as fresh as the last time the SDK
    actually talked to each node — it won't proactively detect a service that just went down
    if nothing has touched it since. Use get_cluster_health_and_services instead when you need
    a live, right-now reachability check; there's also no way to filter this report to specific
    services the way that tool's ping can, since no I/O means nothing to filter.

    For each known endpoint, reports which service it belongs to, its remote/local addresses,
    connection state, and last_activity — how long it's been since that connection last saw
    traffic. Also reports an overall online/degraded/offline cluster state.

    This call makes no request to the server at all, so it needs no specific RBAC role beyond
    whatever the initial cluster connection already required — unlike an active ping, it isn't
    gated on KV/Query/Search or Cluster Admin privileges.

    Returns:
    - Diagnostics report with per-endpoint connection state and overall cluster state
    """
    try:
        cluster = get_cluster_connection(ctx)
        logger.debug("Retrieving cluster diagnostics")
        diagnostics_result = cluster.diagnostics()
        result = diagnostics_result.as_json()

        logger.info("Retrieved cluster diagnostics information")
        return {
            "status": "success",
            "data": json.loads(result),
        }
    except Exception as e:
        logger.error(f"Error getting cluster diagnostics: {e}", exc_info=True)
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get cluster diagnostics information",
        }
