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

import couchbase.subdocument as subdoc
from couchbase.exceptions import CouchbaseException
from fastmcp import Context

from ..utils.connection import connect_to_bucket, format_keyspace
from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import get_cluster_connection
from ..utils.responses import tool_error, tool_success

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.kv")


def get_document_by_id(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
) -> dict[str, Any]:
    """Get a document by its ID from the specified scope and collection.
    If the document is not found, it will raise an exception."""

    keyspace = format_keyspace(bucket_name, scope_name, collection_name)
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
) -> dict[str, Any]:
    """Insert or update a document by its ID.

    IMPORTANT: Only use this tool when the user explicitly requests an 'upsert' operation
    or explicitly states they want to 'insert or update' a document.

    DO NOT use this as a fallback when insert_document_by_id or replace_document_by_id fails.

    Returns {"success": True} on success, or {"success": False, "error": "..."} on
    failure with the reason (e.g. permission denied, network error, invalid content)."""
    keyspace = format_keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Upserting document in {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        collection.upsert(document_id, document_content)
        logger.info(f"Successfully upserted document in {keyspace}")
        return tool_success()
    except Exception as e:
        logger.error(f"Error upserting document in {keyspace}: {e}", exc_info=True)
        return tool_error(e)


def delete_document_by_id(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
) -> dict[str, Any]:
    """Delete a document by its ID.

    Returns {"success": True} on success, or {"success": False, "error": "..."} on
    failure with the reason (e.g. document not found, permission denied, network error)."""
    keyspace = format_keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Deleting document from {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        collection.remove(document_id)
        logger.info(f"Successfully deleted document from {keyspace}")
        return tool_success()
    except Exception as e:
        logger.error(f"Error deleting document from {keyspace}: {e}", exc_info=True)
        return tool_error(e)


def insert_document_by_id(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
    document_content: dict[str, Any],
) -> dict[str, Any]:
    """Insert a new document by its ID. This operation will FAIL if the document already exists.

    IMPORTANT: If this operation fails, DO NOT automatically try replace or upsert.
    Report the failure to the user. They can choose to 'replace' or 'upsert' if desired.

    Returns {"success": True} on success, or {"success": False, "error": "..."} on
    failure with the reason (e.g. document already exists, permission denied, network error)."""
    keyspace = format_keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Inserting document in {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        collection.insert(document_id, document_content)
        logger.info(f"Successfully inserted document in {keyspace}")
        return tool_success()
    except Exception as e:
        logger.error(f"Error inserting document in {keyspace}: {e}", exc_info=True)
        return tool_error(e)


def replace_document_by_id(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
    document_content: dict[str, Any],
) -> dict[str, Any]:
    """Replace an existing document by its ID. This operation will FAIL if the document does not exist.

    IMPORTANT: If this operation fails, DO NOT automatically try insert or upsert.
    Report the failure to the user. They can choose to 'insert' or 'upsert' if desired.

    Returns {"success": True} on success, or {"success": False, "error": "..."} on
    failure with the reason (e.g. document does not exist, permission denied, network error)."""
    keyspace = format_keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        logger.debug(f"Replacing document in {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        collection.replace(document_id, document_content)
        logger.info(f"Successfully replaced document in {keyspace}")
        return tool_success()
    except Exception as e:
        logger.error(f"Error replacing document in {keyspace}: {e}", exc_info=True)
        return tool_error(e)


def sub_document_lookup_in(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
    get_paths: list[str] | None = None,
    exists_paths: list[str] | None = None,
    count_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Look up parts of a document without fetching the whole thing, using Couchbase
    sub-document operations. Use this instead of get_document_by_id when you only need
    a few fields, a presence check, or the size of an array/object inside a document —
    AND you already know the exact field path(s) to look up (e.g. from a prior
    get_document_by_id call on this same document, from the user explicitly naming the
    field, or from a known/confirmed schema for this collection).

    IMPORTANT: Do NOT guess field paths. If you don't already know the document's exact
    field names/structure, call get_document_by_id first (or instead) — a guessed path
    that doesn't exist returns a per-path error here rather than the real data, and
    reporting "not found" for a wrong guess is worse than just fetching the whole
    document and reading the right field.

    Provide one or more of the following. Each is a list of sub-document paths using
    Couchbase's dot/bracket path syntax (e.g. "address.city", "tags[0]", "tags[-1]" for
    the last array element):
    - get_paths: fetch the VALUE at each path.
    - exists_paths: check whether each path exists, without fetching its value (cheaper
      than get_paths — no payload transfer — when you only need a yes/no answer).
    - count_paths: get the number of elements in the array or object at each path (fails
      per-path if the path isn't an array/object).

    At least one of get_paths, exists_paths, or count_paths must be provided. As a rule of
    thumb, keep the combined number of paths across all three to 16 or fewer — Couchbase
    limits subdocument operations per call, though the exact limit is server-side and may
    change. If the server rejects the call (too many paths, or another constraint like path
    length or nesting depth), the whole call fails with {"error": "..."}.

    A path that doesn't exist (or otherwise fails, e.g. count on a non-array/object) does
    NOT fail the whole call — it is reported individually as {"error": ...} in the
    returned dict so the other requested paths can still be resolved.

    Returns a dict with a key for each category that was requested (only requested
    categories are included):
    {
        "get": {"<path>": {"value": <value>} | {"error": "..."}},
        "exists": {"<path>": {"value": true | false} | {"error": "..."}},
        "count": {"<path>": {"value": <count>} | {"error": "..."}},
    }
    On a connection/lookup failure, or an invalid request (no paths / too many paths),
    returns {"error": "<message>"} instead.
    """
    get_paths = get_paths or []
    exists_paths = exists_paths or []
    count_paths = count_paths or []

    specs: list[Any] = []
    spec_meta: list[tuple[str, str]] = []
    for path in get_paths:
        specs.append(subdoc.get(path))
        spec_meta.append(("get", path))
    for path in exists_paths:
        specs.append(subdoc.exists(path))
        spec_meta.append(("exists", path))
    for path in count_paths:
        specs.append(subdoc.count(path))
        spec_meta.append(("count", path))

    keyspace = format_keyspace(bucket_name, scope_name, collection_name)

    if not specs:
        error = (
            "At least one of get_paths, exists_paths, or count_paths must be provided"
        )
        logger.error(f"Error performing sub-document lookup in {keyspace}: {error}")
        return {"error": error}

    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)

    try:
        logger.debug(f"Performing sub-document lookup in {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        result = collection.lookup_in(document_id, specs)
    except Exception as e:
        logger.error(
            f"Error performing sub-document lookup in {keyspace}: {e}", exc_info=True
        )
        return {"error": str(e)}

    response: dict[str, Any] = {}
    for index, (op, path) in enumerate(spec_meta):
        bucket_for_op = response.setdefault(op, {})
        try:
            if op == "exists":
                bucket_for_op[path] = {"value": result.exists(index)}
            else:
                # Identity transform: return the raw value at the path as whatever
                # JSON type it is (get), or the element count (count).
                bucket_for_op[path] = {"value": result.content_as[lambda v: v](index)}
        except CouchbaseException as e:
            bucket_for_op[path] = {"error": str(e)}

    logger.info(f"Successfully performed sub-document lookup in {keyspace}")
    return response
