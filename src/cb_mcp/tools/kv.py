"""
Tools for key-value operations.

This module contains tools for document operations by ID:
- get: Retrieve a document
- upsert: Insert or update a document (creates if not exists, updates if exists)
- insert: Create a document only if it does NOT exist (fails if exists)
- replace: Update a document only if it exists (fails if missing)
- delete: Remove a document
"""

import logging
from typing import Any

from fastmcp import Context

from ..utils.connection import connect_to_bucket
from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import get_cluster_connection

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.kv")


def _keyspace(bucket_name: str, scope_name: str, collection_name: str) -> str:
    """Render a ``bucket.scope.collection`` keyspace string for log context."""
    return f"{bucket_name}.{scope_name}.{collection_name}"


def get_document_by_id(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
) -> dict[str, Any]:
    """Get a document by its ID from the specified scope and collection.
    If the document is not found, it will raise an exception."""

    keyspace = _keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Getting document from {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        result = collection.get(document_id)
        logger.info(f"Retrieved document from {keyspace}")
        return result.content_as[dict]
    except Exception as e:
        logger.error(f"Error getting document from {keyspace}: {e}", exc_info=True)
        raise


def upsert_document_by_id(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
    document_content: dict[str, Any],
) -> bool:
    """Insert or update a document by its ID.

    IMPORTANT: Only use this tool when the user explicitly requests an 'upsert' operation
    or explicitly states they want to 'insert or update' a document.

    DO NOT use this as a fallback when insert_document_by_id or replace_document_by_id fails.

    Returns True on success, False on failure."""
    keyspace = _keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Upserting document in {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        collection.upsert(document_id, document_content)
        logger.info(f"Successfully upserted document in {keyspace}")
        return True
    except Exception as e:
        logger.error(f"Error upserting document in {keyspace}: {e}", exc_info=True)
        return False


def delete_document_by_id(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
) -> bool:
    """Delete a document by its ID.
    Returns True on success, False on failure."""
    keyspace = _keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Deleting document from {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        collection.remove(document_id)
        logger.info(f"Successfully deleted document from {keyspace}")
        return True
    except Exception as e:
        logger.error(f"Error deleting document from {keyspace}: {e}", exc_info=True)
        return False


def insert_document_by_id(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
    document_content: dict[str, Any],
) -> bool:
    """Insert a new document by its ID. This operation will FAIL if the document already exists.

    IMPORTANT: If this operation fails, DO NOT automatically try replace or upsert.
    Report the failure to the user. They can choose to 'replace' or 'upsert' if desired.

    Returns True on success, False on failure (including if document already exists)."""
    keyspace = _keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Inserting document in {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        collection.insert(document_id, document_content)
        logger.info(f"Successfully inserted document in {keyspace}")
        return True
    except Exception as e:
        logger.error(f"Error inserting document in {keyspace}: {e}", exc_info=True)
        return False


def replace_document_by_id(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
    document_content: dict[str, Any],
) -> bool:
    """Replace an existing document by its ID. This operation will FAIL if the document does not exist.

    IMPORTANT: If this operation fails, DO NOT automatically try insert or upsert.
    Report the failure to the user. They can choose to 'insert' or 'upsert' if desired.

    Returns True on success, False on failure (including if document does not exist)."""
    keyspace = _keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Replacing document in {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        collection.replace(document_id, document_content)
        logger.info(f"Successfully replaced document in {keyspace}")
        return True
    except Exception as e:
        logger.error(f"Error replacing document in {keyspace}: {e}", exc_info=True)
        return False
