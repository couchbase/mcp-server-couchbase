"""
Tools for server operations.

This module contains tools for getting the server status, testing the connection, and getting the buckets in the cluster, the scopes and collections in the bucket.
"""

import json
import logging
from typing import Any

from fastmcp import Context

from ..utils.config import get_settings
from ..utils.connection import connect_to_bucket
from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import get_cluster_connection, get_cluster_provider
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
    }

    connection_status = {
        "cluster_connected": (
            provider.is_connected(ctx) if provider is not None else False
        ),
    }

    return {
        "server_name": MCP_SERVER_NAME,
        "status": "running",
        "configuration": configuration,
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
        scopes_collections = {}
        collection_manager = bucket.collections()
        scopes = collection_manager.get_all_scopes()
        for scope in scopes:
            collection_names = [c.name for c in scope.collections]
            scopes_collections[scope.name] = collection_names
        return scopes_collections
    except Exception as e:
        logger.error(f"Error getting scopes and collections: {e}")
        raise


def get_buckets_in_cluster(ctx: Context) -> list[str]:
    """Get the names of all the accessible buckets in the cluster."""
    cluster = get_cluster_connection(ctx)
    bucket_manager = cluster.buckets()
    buckets_with_settings = bucket_manager.get_all_buckets()

    buckets = []
    for bucket in buckets_with_settings:
        buckets.append(bucket.name)

    return buckets


def get_scopes_in_bucket(ctx: Context, bucket_name: str) -> list[str]:
    """Get the names of all scopes in the given bucket."""
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        scopes = bucket.collections().get_all_scopes()
        return [scope.name for scope in scopes]
    except Exception as e:
        logger.error(f"Error getting scopes in the bucket {bucket_name}: {e}")
        raise


def get_collections_in_scope(
    ctx: Context, bucket_name: str, scope_name: str
) -> list[str]:
    """Get the names of all collections in the given scope and bucket."""

    # Get the collections in the scope using system:all_keyspaces collection
    query = "SELECT DISTINCT(name) as collection_name FROM system:all_keyspaces where `bucket`=$bucket_name and `scope`=$scope_name"
    results = run_cluster_query(
        ctx, query, bucket_name=bucket_name, scope_name=scope_name
    )
    return [result["collection_name"] for result in results]


def get_cluster_health_and_services(
    ctx: Context, bucket_name: str | None = None
) -> dict[str, Any]:
    """Get cluster health status and list of all running services.

    This tool provides health monitoring by:
    - Getting health status of all running services with latency information (via ping)
    - Listing all services running on the cluster with their endpoints
    - Showing connection status and node information for each service

    If bucket_name is provided, it actively pings services from the perspective of the bucket.
    Otherwise, it uses cluster-level ping to get the health status of the cluster.

    Returns:
    - Cluster health status with service-level connection details and latency measurements
    """
    try:
        cluster = get_cluster_connection(ctx)

        if bucket_name:
            # Ping services from the perspective of the bucket
            bucket = connect_to_bucket(cluster, bucket_name)
            ping_result = bucket.ping()
            result = ping_result.as_json()
        else:
            # Ping services from the perspective of the cluster
            ping_result = cluster.ping()
            result = ping_result.as_json()

        return {
            "status": "success",
            "data": json.loads(result),
        }
    except Exception as e:
        logger.error(f"Error getting cluster health: {e}")
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to get cluster health and services information",
        }
