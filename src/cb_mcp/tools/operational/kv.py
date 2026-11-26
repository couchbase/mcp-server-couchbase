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
import os
from typing import Any

import couchbase.subdocument as subdoc
from couchbase.exceptions import CouchbaseException
from fastmcp import Context

from ...servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ...utils.operational.connection import connect_to_bucket, format_keyspace
from ...utils.operational.context import get_cluster_connection
from ...utils.responses import error_message, tool_error, tool_success

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.tools.kv")


#: Upper bound on a single batch read. Enforced before the cluster is touched.
#: Lower than the cap on batch writes on purpose: on a write the agent already
#: holds the documents, while on a read they come back into its context and stay
#: there. Worth stating plainly in review: SQL++ `USE KEYS` carries no equivalent
#: cap on keys accepted or bytes returned, so if result size is the concern, the
#: bounded option is this one.
MAX_BULK_READ_DOCS = 50

#: Default bound on the combined size of the documents one batch read returns,
#: measured as the JSON text of the returned documents. 64 KiB is on the order of
#: 16,000 tokens at a rough four bytes per token: enough for a useful batch of
#: ordinary documents, small enough that one call cannot consume a context window.
DEFAULT_BULK_READ_BYTES = 64 * 1024

#: The most a deployment can raise the bound to. A deployment may lower the bound
#: freely and may raise it up to here, never past it.
MAX_BULK_READ_BYTES_CEILING = 128 * 1024

#: Environment variable a deployment sets to change the bound.
BULK_READ_BYTES_ENV_VAR = "CB_MCP_BULK_READ_MAX_BYTES"


def bulk_read_byte_limit() -> int:
    """The byte bound in force: the default, or the deployment's value clamped.

    A value that is not a positive integer is ignored rather than trusted, and a
    value above the ceiling is held at the ceiling, so a misconfiguration cannot
    turn the guardrail off.
    """
    raw = os.environ.get(BULK_READ_BYTES_ENV_VAR)
    if raw is None:
        return DEFAULT_BULK_READ_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BULK_READ_BYTES
    if value < 1:
        return DEFAULT_BULK_READ_BYTES
    return min(value, MAX_BULK_READ_BYTES_CEILING)


def _check_read_batch(
    *, single: Any, batch: Any, keyspace: str
) -> dict[str, Any] | None:
    """Validate the single/batch choice. Returns an error envelope, or None.

    The two modes are mutually exclusive on purpose. A caller that supplied both
    has not decided what it is asking for, and silently preferring one hides
    that rather than surfacing it.
    """
    if batch is None:
        if single is None:
            return tool_error(
                "Provide either document_id for one document, or document_ids "
                "for several. Neither was given.",
                keyspace=keyspace,
            )
        return None

    if single is not None:
        return tool_error(
            "Provide either document_id or document_ids, not both.",
            keyspace=keyspace,
        )

    if not batch:
        return tool_error("document_ids is empty. Nothing to read.", keyspace=keyspace)

    if len(batch) > MAX_BULK_READ_DOCS:
        return tool_error(
            f"document_ids holds {len(batch)} entries, which is more than the "
            f"limit of {MAX_BULK_READ_DOCS}. Nothing was read. Ask for fewer "
            "documents, or narrow the set first.",
            keyspace=keyspace,
            requested=len(batch),
            limit=MAX_BULK_READ_DOCS,
        )
    return None


def _shape_multi_get(
    result: Any, requested_ids: list[str], max_bytes: int | None = None
) -> dict[str, Any]:
    """Turn an SDK multi-get into a complete account of what was asked for.

    Every requested id lands in exactly one of ``documents``, ``errors``,
    ``oversize`` and ``not_returned``. That is the difference from the SQL++
    alternative, and it is the point of the shape: ``SELECT * ... USE KEYS``
    simply produces no row for a key that is absent, so the caller has to diff
    what it asked for against what came back, and still cannot tell "absent"
    from "I mistyped it".

    A document the SDK reports neither way is recorded as an error rather than
    quietly dropped, for the same reason: a missing outcome is not an answer.

    The combined size of the returned documents is bounded by ``max_bytes``.
    Documents are added in request order. Truncation is never silent:

    * a document that is larger than the whole bound is never cut. It goes in
      ``oversize`` with its size, to be read on its own with ``document_id``;
    * once the next document would take the total past the bound, it and every
      later document that was read successfully go in ``not_returned``, in
      request order, so the caller can ask for exactly those next;
    * ``truncated`` and ``bytes_returned`` say whether and how much.
    """
    if max_bytes is None:
        max_bytes = bulk_read_byte_limit()
    exceptions = dict(getattr(result, "exceptions", None) or {})
    results = getattr(result, "results", None) or {}

    documents: dict[str, Any] = {}
    errors: dict[str, str] = {}
    oversize: dict[str, int] = {}
    not_returned: list[str] = []
    used_bytes = 0

    for doc_id in requested_ids:
        if doc_id in exceptions:
            exc = exceptions[doc_id]
            # Through the shared formatter, so up to 50 failures cannot inflate
            # one response past the cap every other tool error observes.
            errors[doc_id] = error_message(
                f"{type(exc).__name__}: {error_message(exc)}".strip(": ")
            )
            continue
        if doc_id in results:
            try:
                content = results[doc_id].content_as[dict]
                size = len(json.dumps(content).encode("utf-8"))
            except Exception as e:
                errors[doc_id] = error_message(
                    "The document was returned but could not be read as JSON: "
                    f"{type(e).__name__}: {error_message(e)}".strip(": ")
                )
                continue
            if size > max_bytes:
                oversize[doc_id] = size
                continue
            if not_returned or used_bytes + size > max_bytes:
                not_returned.append(doc_id)
                continue
            used_bytes += size
            documents[doc_id] = content
            continue
        errors[doc_id] = (
            "The server returned no outcome for this document. It was not read; "
            "this does not establish that it is absent."
        )

    fields: dict[str, Any] = {
        "documents": documents,
        "errors": errors,
        "oversize": oversize,
        "not_returned": not_returned,
        "truncated": bool(not_returned),
        "bytes_returned": used_bytes,
        "limit_bytes": max_bytes,
    }
    if oversize:
        fields["oversize_guidance"] = (
            f"Each document in oversize is larger than the {max_bytes} byte limit "
            "and was not returned. Read it on its own with document_id."
        )
    if not_returned:
        fields["not_returned_guidance"] = (
            "The result reached the byte limit. Call again with document_ids "
            "set to not_returned to get the rest."
        )
    return tool_success(**fields)


def get_document_by_id(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    document_id: str | None = None,
    document_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Get a document by its ID from the specified scope and collection.

    Pass EITHER document_id for one document, OR document_ids for up to 50
    documents in a single call. The returned documents together are limited to
    64 KiB, so a call cannot flood your context. Prefer the batch form whenever
    you need several documents: it costs one tool call instead of one per
    document, and the result says explicitly what happened to each one.

    Single form returns the document, and raises if it is not found.
    Batch form returns {"success": True, "documents": {id: content},
    "errors": {id: reason}, "oversize": {id: bytes}, "not_returned": [ids],
    "truncated": bool, "bytes_returned": int, "limit_bytes": int}, where every
    requested ID appears in exactly one of documents, errors, oversize and
    not_returned, and it never raises for an individual document. If truncated
    is true, call again with document_ids set to not_returned. A document listed
    in oversize is larger than the whole limit: read it alone with document_id."""

    keyspace = format_keyspace(bucket_name, scope_name, collection_name)
    invalid = _check_read_batch(
        single=document_id, batch=document_ids, keyspace=keyspace
    )
    if invalid is not None:
        return invalid

    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    try:
        collection = bucket.scope(scope_name).collection(collection_name)

        if document_ids is not None:
            # Deduplicated, order preserved. Asking twice is one read, and
            # reporting the repeat separately would describe the caller's
            # typing rather than the collection.
            unique_ids = list(dict.fromkeys(document_ids))
            logger.debug(f"Getting {len(unique_ids)} document(s) from {keyspace}")
            result = collection.get_multi(unique_ids, return_exceptions=True)
            shaped = _shape_multi_get(result, unique_ids)
            logger.info(
                f"Read batch from {keyspace}: "
                f"{len(shaped['documents'])} returned, {len(shaped['errors'])} errors, "
                f"{len(shaped['oversize'])} oversize, "
                f"{len(shaped['not_returned'])} not returned"
            )
            return shaped

        logger.debug(f"Getting document from {keyspace}")
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


def lookup_subdocument(
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


def mutate_subdocument(
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
    mutate (e.g. from a prior get_document_by_id or lookup_subdocument call, from the
    user explicitly naming the field, or from a known schema). Do NOT guess field paths.

    IMPORTANT — atomicity: a mutate_in call is all-or-nothing. If ANY requested spec fails
    (e.g. an insert on a path that already exists), the ENTIRE call fails and NONE of the
    mutations are applied — unlike lookup_subdocument, there is no partial success.

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
    for every category except replace_specs, remove_paths and array_insert_specs, which
    always require the full path (for array_insert, the array itself) to already exist.
    The server doesn't support create_parents on array inserts, so it is ignored for them.

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
            lambda s: subdoc.array_insert(s["path"], *s["values"]),
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
            lambda s: (
                subdoc.increment(s["path"], s["delta"], create_parents=create_parents)
                if s["delta"] >= 0
                else subdoc.decrement(
                    s["path"], abs(s["delta"]), create_parents=create_parents
                )
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
                except Exception:  # noqa: S112 (expected fallback attempt, not a swallowed bug)
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
