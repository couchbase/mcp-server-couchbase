"""
Tools for scope and collection management.

This module contains write tools that create and delete scopes and collections
via the Couchbase SDK collection manager (``bucket.collections()``). They are
write operations — not loaded when READ_ONLY_MODE is True — and require the
``couchbase-mcp:write`` OAuth scope. Applicable to Couchbase Server 7.6+ and
Capella.
"""

import logging
from typing import Any

from fastmcp import Context

from ..utils.connection import connect_to_bucket
from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import get_cluster_connection
from ..utils.responses import tool_error, tool_success

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.collection_management")


def create_scope(ctx: Context, bucket_name: str, scope_name: str) -> dict[str, Any]:
    """Create a new scope in a bucket.

    Returns {"success": True, ...} on success, or {"success": False,
    "error": ...} on failure — e.g. the scope already exists.
    """
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Creating scope '{scope_name}' in bucket '{bucket_name}'")
        bucket.collections().create_scope(scope_name)
        logger.info(f"Created scope '{scope_name}' in bucket '{bucket_name}'")
        return tool_success(
            bucket_name=bucket_name,
            scope_name=scope_name,
            message=f"Created scope '{scope_name}' in bucket '{bucket_name}'.",
        )
    except Exception as e:
        logger.error(
            f"Error creating scope '{scope_name}' in bucket '{bucket_name}': {e}",
            exc_info=True,
        )
        return tool_error(
            e,
            message=f"Failed to create scope '{scope_name}' in bucket '{bucket_name}'.",
        )


def create_collection(
    ctx: Context, bucket_name: str, scope_name: str, collection_name: str
) -> dict[str, Any]:
    """Create a new collection in an existing scope.

    Returns {"success": True, ...} on success, or {"success": False,
    "error": ...} on failure — e.g. the collection already exists or the scope
    does not exist.
    """
    keyspace = f"{bucket_name}.{scope_name}.{collection_name}"
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Creating collection {keyspace}")
        bucket.collections().create_collection(scope_name, collection_name)
        logger.info(f"Created collection {keyspace}")
        return tool_success(
            bucket_name=bucket_name,
            scope_name=scope_name,
            collection_name=collection_name,
            message=f"Created collection {keyspace}.",
        )
    except Exception as e:
        logger.error(f"Error creating collection {keyspace}: {e}", exc_info=True)
        return tool_error(e, message=f"Failed to create collection {keyspace}.")


def delete_scope(ctx: Context, bucket_name: str, scope_name: str) -> dict[str, Any]:
    """Delete an existing scope from a bucket.

    This permanently removes the scope AND every collection (and all documents)
    within it, and cannot be undone.

    Returns {"success": True, ...} on success, or {"success": False,
    "error": ...} on failure — e.g. the scope does not exist.
    """
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Dropping scope '{scope_name}' in bucket '{bucket_name}'")
        bucket.collections().drop_scope(scope_name)
        logger.info(f"Dropped scope '{scope_name}' in bucket '{bucket_name}'")
        return tool_success(
            bucket_name=bucket_name,
            scope_name=scope_name,
            message=f"Deleted scope '{scope_name}' from bucket '{bucket_name}'.",
        )
    except Exception as e:
        logger.error(
            f"Error dropping scope '{scope_name}' in bucket '{bucket_name}': {e}",
            exc_info=True,
        )
        return tool_error(
            e,
            message=f"Failed to delete scope '{scope_name}' from bucket '{bucket_name}'.",
        )


def delete_collection(
    ctx: Context, bucket_name: str, scope_name: str, collection_name: str
) -> dict[str, Any]:
    """Delete an existing collection from a scope.

    This permanently removes the collection and all its documents, and cannot be
    undone.

    Returns {"success": True, ...} on success, or {"success": False,
    "error": ...} on failure — e.g. the collection does not exist.
    """
    keyspace = f"{bucket_name}.{scope_name}.{collection_name}"
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Dropping collection {keyspace}")
        bucket.collections().drop_collection(scope_name, collection_name)
        logger.info(f"Dropped collection {keyspace}")
        return tool_success(
            bucket_name=bucket_name,
            scope_name=scope_name,
            collection_name=collection_name,
            message=f"Deleted collection {keyspace}.",
        )
    except Exception as e:
        logger.error(f"Error dropping collection {keyspace}: {e}", exc_info=True)
        return tool_error(e, message=f"Failed to delete collection {keyspace}.")
