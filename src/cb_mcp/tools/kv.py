"""
Tools for key-value operations.

This module contains tools for document operations by ID:
- get: Retrieve a document
- upsert: Insert or update a document (creates if not exists, updates if exists)
- insert: Create a document only if it does NOT exist (fails if exists)
- replace: Update a document only if it exists (fails if missing)
- delete: Remove a document
"""

import json
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


def sub_document_mutate_in(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str,
    upsert_specs: list[dict[str, Any]] | None = None,
    insert_specs: list[dict[str, Any]] | None = None,
    replace_specs: list[dict[str, Any]] | None = None,
    remove_paths: list[str] | None = None,
    array_append_specs: list[dict[str, Any]] | None = None,
    array_prepend_specs: list[dict[str, Any]] | None = None,
    array_insert_specs: list[dict[str, Any]] | None = None,
    array_add_unique_specs: list[dict[str, Any]] | None = None,
    counter_specs: list[dict[str, Any]] | None = None,
    create_parents: bool = False,
) -> dict[str, Any]:
    """Modify parts of an EXISTING document without rewriting the whole thing, using
    Couchbase sub-document mutation operations. The document must already exist — use
    upsert_document_by_id first if it doesn't.

    Use this instead of upsert_document_by_id/replace_document_by_id when you only need to
    change, add, or remove a few fields — AND you already know the exact field path(s) to
    mutate (e.g. from a prior get_document_by_id or sub_document_lookup_in call, from the
    user explicitly naming the field, or from a known schema). Do NOT guess field paths.

    IMPORTANT — atomicity: a mutate_in call is all-or-nothing. If ANY requested spec fails
    (e.g. an insert on a path that already exists), the ENTIRE call fails and NONE of the
    mutations are applied — unlike sub_document_lookup_in, there is no partial success.

    Provide one or more of the following lists. Each path uses Couchbase's dot/bracket path
    syntax (e.g. "address.city", "tags[0]", "tags[-1]" for the last array element):
    - upsert_specs: [{"path": str, "value": <any JSON value>}, ...] — set the value at each
      path, creating the field if it doesn't exist.
    - insert_specs: [{"path": str, "value": <any JSON value>}, ...] — create the field at
      each path; fails if a path already exists.
    - replace_specs: [{"path": str, "value": <any JSON value>}, ...] — set the value at each
      path; fails if a path doesn't already exist.
    - remove_paths: [str, ...] — delete the field at each path; fails if a path doesn't exist.
    - array_append_specs / array_prepend_specs: [{"path": str, "values": [<any JSON value>,
      ...]}, ...] — add one or more values to the end/start of the array at each path.
    - array_insert_specs: [{"path": str, "values": [<any JSON value>, ...]}, ...] — insert
      one or more values at a specific array index; path must point at an index, e.g.
      "tags[2]".
    - array_add_unique_specs: [{"path": str, "value": <scalar: str|int|float|bool>}, ...] —
      add a scalar value to the array at each path only if it isn't already present; fails
      if the value is already in the array.
    - counter_specs: [{"path": str, "delta": int}, ...] — increment (delta >= 0) or
      decrement (delta < 0) the integer at each path by delta, returning the new value. If
      the field doesn't exist yet, it is created (and its parents, if create_parents=True).

    create_parents: if True, missing intermediate path segments are created automatically
    for every category except replace_specs and remove_paths, which always require the full
    path to already exist.

    At least one spec must be provided. As a rule of thumb, keep the combined number of
    specs across all categories to 16 or fewer — Couchbase limits subdocument operations
    per call, though the exact limit is server-side and may change. If the server rejects
    the call, the whole call fails with {"error": "..."} and nothing is mutated.

    Returns a dict with a key for each requested category, mapping path -> {"success": true}
    (or, for counter_specs, {"success": true, "value": <new value>}):
    {
        "upsert": {"<path>": {"success": true}},
        "counter": {"<path>": {"success": true, "value": <new value>}},
        ...
    }
    On a connection/mutation failure, or an invalid request, returns {"error": "<message>"}
    instead and no mutations are applied.
    """
    keyspace = format_keyspace(bucket_name, scope_name, collection_name)

    specs: list[Any] = []
    spec_meta: list[tuple[str, str]] = []
    # (category, requested specs, builder) — table-driven so building the flat spec list
    # doesn't require a separate branch per category.
    value_categories: list[tuple[str, list[dict[str, Any]], Any]] = [
        (
            "upsert",
            upsert_specs or [],
            lambda s: subdoc.upsert(
                s["path"], s["value"], create_parents=create_parents
            ),
        ),
        (
            "insert",
            insert_specs or [],
            lambda s: subdoc.insert(
                s["path"], s["value"], create_parents=create_parents
            ),
        ),
        (
            "replace",
            replace_specs or [],
            lambda s: subdoc.replace(s["path"], s["value"]),
        ),
        (
            "array_append",
            array_append_specs or [],
            lambda s: subdoc.array_append(
                s["path"], *s["values"], create_parents=create_parents
            ),
        ),
        (
            "array_prepend",
            array_prepend_specs or [],
            lambda s: subdoc.array_prepend(
                s["path"], *s["values"], create_parents=create_parents
            ),
        ),
        (
            "array_insert",
            array_insert_specs or [],
            lambda s: subdoc.array_insert(
                s["path"], *s["values"], create_parents=create_parents
            ),
        ),
        (
            "array_add_unique",
            array_add_unique_specs or [],
            lambda s: subdoc.array_addunique(
                s["path"], s["value"], create_parents=create_parents
            ),
        ),
        (
            "counter",
            counter_specs or [],
            lambda s: subdoc.increment(
                s["path"], s["delta"], create_parents=create_parents
            )
            if s["delta"] >= 0
            else subdoc.decrement(
                s["path"], abs(s["delta"]), create_parents=create_parents
            ),
        ),
    ]

    try:
        for category, category_specs, build_spec in value_categories:
            for spec in category_specs:
                specs.append(build_spec(spec))
                spec_meta.append((category, spec["path"]))
        for path in remove_paths or []:
            specs.append(subdoc.remove(path))
            spec_meta.append(("remove", path))
    except (KeyError, TypeError, CouchbaseException) as e:
        error = f"Invalid mutation spec: {e}"
        logger.warning(f"Error building sub-document mutation for {keyspace}: {error}")
        return {"error": error}

    if not specs:
        error = "At least one mutation spec must be provided"
        logger.warning(f"Error performing sub-document mutation in {keyspace}: {error}")
        return {"error": error}

    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)

    try:
        logger.debug(f"Performing sub-document mutation in {keyspace}")
        collection = bucket.scope(scope_name).collection(collection_name)
        result = collection.mutate_in(document_id, specs)
    except Exception as e:
        error_context = getattr(e, "error_context", None)
        failed_index = getattr(error_context, "first_error_index", None)
        detail = ""
        if failed_index is not None and 0 <= failed_index < len(spec_meta):
            detail = f" (spec {failed_index}: {spec_meta[failed_index][1]})"
        logger.error(
            f"Error performing sub-document mutation in {keyspace}: {e}{detail}",
            exc_info=True,
        )
        return {"error": f"{e}{detail}"}

    response: dict[str, Any] = {}
    for index, (op, path) in enumerate(spec_meta):
        bucket_for_op = response.setdefault(op, {})
        if op == "counter":
            # The installed SDK constructs MutateInResult without a transcoder
            # (unlike LookupInResult), which makes content_as always raise for
            # mutate_in results — fall back to decoding the raw field value.
            new_value = None
            for read_new_value in (
                lambda index=index: result.content_as[int](index),
                lambda index=index: int(
                    json.loads(result._orig.raw_result["fields"][index]["value"])
                ),
            ):
                try:
                    new_value = read_new_value()
                    break
                except Exception:
                    continue

            if new_value is None:
                logger.warning(
                    f"Counter mutation at '{path}' in {keyspace} committed but "
                    "its new value could not be read from the SDK result."
                )
                bucket_for_op[path] = {"success": True}
                continue
            bucket_for_op[path] = {"success": True, "value": new_value}
            continue
        bucket_for_op[path] = {"success": True}

    logger.info(f"Successfully performed sub-document mutation in {keyspace}")
    return response
