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

from ..utils.connection import connect_to_bucket
from ..utils.constants import MCP_SERVER_NAME
from ..utils.context import get_cluster_connection

# Couchbase server limit: max subdocument operations combined in a single
# lookup_in or mutate_in call.
MAX_SUBDOC_SPECS = 16

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

    IMPORTANT: Do NOT guess field paths. This tool has no knowledge of the document's
    schema — it does not know whether a field exists, what it's named, or how it's
    nested. A question phrased in plain English does NOT tell you the underlying field
    name or shape (a question about "how many X" could map to a top-level count field
    like "x_count" rather than a nested array like "x.items") — do not infer a path
    from the wording of the question. Unless you already know the exact path from a
    prior get_document_by_id call on this same document, from the user explicitly
    naming the field, or from a known/confirmed schema for this collection, call
    get_document_by_id first (or instead) — a guessed path that doesn't exist returns a
    per-path error here rather than the real data, and reporting "not found" for a
    wrong guess is worse than just fetching the whole document and reading the right
    field.

    Provide one or more of the following. Each is a list of sub-document paths using
    Couchbase's dot/bracket path syntax (e.g. "address.city", "tags[0]", "tags[-1]" for
    the last array element):
    - get_paths: fetch the VALUE at each path.
    - exists_paths: check whether each path exists, without fetching its value (cheaper
      than get_paths — no payload transfer — when you only need a yes/no answer).
    - count_paths: get the number of elements in the array or object at each path (fails
      per-path if the path isn't an array/object).

    At least one of get_paths, exists_paths, or count_paths must be provided, and the
    combined number of paths across all three cannot exceed 16 (a Couchbase server limit
    on subdocument operations per call). Paths cannot exceed 1024 characters or 32 levels
    of nesting. Violating either of these returns {"error": "..."} — no paths are looked up.

    A path that doesn't exist (or otherwise fails, e.g. count on a non-array/object) does
    NOT fail the whole call — it is reported individually as {"success": False, "error": ...}
    in the returned dict so the other requested paths can still be resolved.

    Returns a dict with a key for each category that was requested (only requested
    categories are included):
    {
        "get": {"<path>": {"success": true, "value": <value>} | {"success": false, "error": "..."}},
        "exists": {"<path>": true | false},
        "count": {"<path>": {"success": true, "value": <count>} | {"success": false, "error": "..."}},
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

    keyspace = _keyspace(bucket_name, scope_name, collection_name)

    if not specs:
        error = (
            "At least one of get_paths, exists_paths, or count_paths must be provided"
        )
        logger.error(f"Error performing sub-document lookup in {keyspace}: {error}")
        return {"error": error}
    if len(specs) > MAX_SUBDOC_SPECS:
        error = (
            f"Too many sub-document paths requested ({len(specs)}). Couchbase allows at "
            f"most {MAX_SUBDOC_SPECS} operations combined in a single lookup_in call."
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
        if op == "exists":
            try:
                bucket_for_op[path] = result.exists(index)
            except CouchbaseException as e:
                bucket_for_op[path] = {"success": False, "error": str(e)}
            continue
        try:
            value = result.content_as[lambda v: v](index)
            bucket_for_op[path] = {"success": True, "value": value}
        except CouchbaseException as e:
            bucket_for_op[path] = {
                "success": False,
                "error": str(e),
            }

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
    Couchbase sub-document mutation operations. The document itself must already exist —
    this tool does not create it (use upsert_document_by_id first if it doesn't exist yet).

    Use this instead of upsert_document_by_id/replace_document_by_id when you only need to
    change, add, or remove a few fields — AND you already know the exact field path(s) to
    mutate (e.g. from a prior get_document_by_id or sub_document_lookup_in call on this same
    document, from the user explicitly naming the field, or from a known/confirmed schema
    for this collection). Do NOT guess field paths.

    IMPORTANT — atomicity: a single mutate_in call is all-or-nothing. If ANY requested spec
    fails (e.g. an insert on a path that already exists, a replace on a path that doesn't),
    the ENTIRE call fails and NONE of the requested mutations are applied — unlike
    sub_document_lookup_in, there is no partial success across specs in one call.

    Provide one or more of the following lists. Each path uses Couchbase's dot/bracket path
    syntax (e.g. "address.city", "tags[0]", "tags[-1]" for the last array element):
    - upsert_specs: [{"path": str, "value": <any JSON value>}, ...] — set the value at each
      path, creating the field if it doesn't exist.
    - insert_specs: [{"path": str, "value": <any JSON value>}, ...] — create the field at
      each path; fails (the whole call) if a path already exists.
    - replace_specs: [{"path": str, "value": <any JSON value>}, ...] — set the value at each
      path; fails (the whole call) if a path does not already exist.
    - remove_paths: [str, ...] — delete the field at each path; fails (the whole call) if a
      path does not exist.
    - array_append_specs / array_prepend_specs: [{"path": str, "values": [<any JSON value>,
      ...]}, ...] — add one or more values to the end/start of the array at each path.
    - array_insert_specs: [{"path": str, "values": [<any JSON value>, ...]}, ...] — insert
      one or more values at a specific array index; path must point at an index, e.g.
      "tags[2]".
    - array_add_unique_specs: [{"path": str, "value": <scalar: str|int|float|bool>}, ...] —
      add a single scalar value to the array at each path only if it isn't already present;
      fails (the whole call) if the value is already in the array.
    - counter_specs: [{"path": str, "delta": int}, ...] — increment (delta >= 0) or
      decrement (delta < 0) the integer at each path by delta, returning the new value.
      The existing value and the result must fit in a signed 64-bit integer
      (-9223372036854775807 to 9223372036854775807) — this is a narrower range than
      full-document counters, and unlike full-document counters this fails (the whole
      call) on overflow/underflow instead of wrapping or silently clamping to 0. If the
      field doesn't exist yet, it is created (and its parents, if create_parents=True).

    create_parents: if True, missing intermediate path segments are created automatically
    for upsert_specs, insert_specs, array_append_specs, array_prepend_specs,
    array_insert_specs, array_add_unique_specs, and counter_specs. It has no effect on
    replace_specs or remove_paths, which always require the full path to already exist.

    At least one spec must be provided, and the combined number of specs across all
    categories cannot exceed 16 (a Couchbase server limit on subdocument operations per
    call, shared with sub_document_lookup_in). Paths cannot exceed 1024 characters or 32
    levels of nesting. Violating either of these returns {"error": "..."} and nothing is
    mutated.

    Returns a dict with a key for each category that was requested (only requested
    categories are included), each mapping path -> {"success": true} (or, for
    counter_specs, {"success": true, "value": <new value>}):
    {
        "upsert": {"<path>": {"success": true}},
        "counter": {"<path>": {"success": true, "value": <new value>}},
        ...
    }
    On a connection/mutation failure, or an invalid request (no specs / too many specs),
    returns {"error": "<message>"} instead and no mutations are applied.
    """
    keyspace = _keyspace(bucket_name, scope_name, collection_name)

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
        logger.error(f"Error building sub-document mutation for {keyspace}: {error}")
        return {"error": error}

    if not specs:
        error = "At least one mutation spec must be provided"
        logger.error(f"Error performing sub-document mutation in {keyspace}: {error}")
        return {"error": error}
    if len(specs) > MAX_SUBDOC_SPECS:
        error = (
            f"Too many sub-document mutations requested ({len(specs)}). Couchbase allows "
            f"at most {MAX_SUBDOC_SPECS} operations combined in a single mutate_in call."
        )
        logger.error(f"Error performing sub-document mutation in {keyspace}: {error}")
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
            try:
                new_value = result.content_as[int](index)
            except Exception:
                raw_value = result._orig.raw_result["fields"][index]["value"]
                new_value = int(json.loads(raw_value))
            bucket_for_op[path] = {"success": True, "value": new_value}
            continue
        bucket_for_op[path] = {"success": True}

    logger.info(f"Successfully performed sub-document mutation in {keyspace}")
    return response
