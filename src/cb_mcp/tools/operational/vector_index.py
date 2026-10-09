"""Vector (and scalar) GSI index creation tools.

Covers the index-creation feature `index.py`'s `create_index` cannot: Hyperscale
and Composite Vector Indexes have no Couchbase Python SDK management API
(confirmed against the installed SDK's `CreateQueryIndexOptions`, which has no
`VECTOR`/`WITH`-clause support) -- both are created with raw SQL++ DDL instead.
`create_query_index` covers `create_index`'s entire surface too
(`index_type="scalar"`), so one tool handles every GSI index-creation shape
through one execution path, rather than an SDK-manager path for scalar glued to
a raw-SQL++ path for vector only.

`create_index` is deprecated in favor of this tool (see its own docstring) but
not removed -- kept for backward compatibility, to be removed in a future 2.0
release.

Error handling follows `create_index`'s convention: `get_cluster_connection`
and `connect_to_bucket` are called outside the `try` block and propagate
uncaught (can't reach the thing we were asked to create an index on); every
validation failure and query error past that point is returned as
`tool_error` rather than raised.
"""

import json
import logging
from typing import Any

from fastmcp import Context

from ...servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ...utils.operational.connection import connect_to_bucket, format_keyspace
from ...utils.operational.context import get_cluster_connection
from ...utils.responses import tool_error, tool_success
from ...utils.sqlpp import safe_field_path, safe_ident

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.tools.vector_index")

_VALID_INDEX_TYPES = frozenset({"scalar", "hyperscale_vector", "composite_vector"})

# Case-insensitive GSI similarity values. Deliberately distinct from the
# Search (FTS) service's own enum (cosine/dot_product/l2_norm, lowercase) --
# the two are not interchangeable, and "DOT_PRODUCT" (FTS's spelling) is a
# real, common point of confusion for GSI, which uses "DOT" instead.
_VALID_SIMILARITIES = frozenset(
    {"EUCLIDEAN_SQUARED", "EUCLIDEAN", "L2_SQUARED", "L2", "COSINE", "DOT"}
)

# Fields create_query_index already manages explicitly -- with_options may
# not override any of these (see _build_with_clause).
_RESERVED_WITH_KEYS = frozenset(
    {"defer_build", "num_replicas", "dimension", "similarity", "description"}
)


def _reject_vector_only_params(
    vector_field: str | None,
    dimension: int | None,
    similarity: str | None,
    description: str | None,
    include: list[str] | None,
    vector_index_position: int | None,
) -> None:
    """Raise if any vector-only param was passed for index_type='scalar'."""
    if vector_field is not None:
        raise ValueError("vector_field is only valid for a vector index_type")
    if dimension is not None:
        raise ValueError("dimension is only valid for a vector index_type")
    if similarity is not None:
        raise ValueError("similarity is only valid for a vector index_type")
    if description is not None:
        raise ValueError("description is only valid for a vector index_type")
    if include:
        raise ValueError("include is only valid for index_type='hyperscale_vector'")
    if vector_index_position is not None:
        raise ValueError(
            "vector_index_position is only valid for index_type='composite_vector'"
        )


def _require_vector_fields(
    index_type: str,
    vector_field: str | None,
    dimension: int | None,
    similarity: str | None,
) -> None:
    """Validate the fields every vector index_type requires.

    Pure validation -- raises ValueError on any violation, returns nothing.
    """
    if not vector_field:
        raise ValueError(f"vector_field is required for index_type={index_type!r}")
    if dimension is None:
        raise ValueError(
            f"dimension is required for index_type={index_type!r} -- Couchbase "
            "does not infer it. Determine it from your embedding model's known "
            "output size, or inspect a real document (e.g. via get_document_by_id) "
            "rather than guessing."
        )
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
        raise ValueError(f"dimension must be a positive integer, got {dimension!r}")
    if not similarity:
        raise ValueError(
            f"similarity is required for index_type={index_type!r} -- it must "
            "match whatever metric you will query with (run_vector_search's "
            "distance_metric), and Couchbase's own default if omitted "
            "(L2_SQUARED) is usually not what an embedding model wants. Valid "
            f"values (case-insensitive): {sorted(_VALID_SIMILARITIES)}. Note "
            "'DOT_PRODUCT' is FTS's spelling, not GSI's -- use 'DOT' here."
        )
    if similarity.upper() not in _VALID_SIMILARITIES:
        raise ValueError(
            f"similarity must be one of {sorted(_VALID_SIMILARITIES)} "
            f"(case-insensitive), got {similarity!r}. Note 'DOT_PRODUCT' is FTS's "
            "spelling, not GSI's -- use 'DOT' here."
        )


def _validate_params(
    index_type: str,
    keys: list[str] | None,
    vector_field: str | None,
    dimension: int | None,
    similarity: str | None,
    description: str | None,
    include: list[str] | None,
    vector_index_position: int | None,
) -> None:
    """Enforce the per-index_type required/forbidden parameter matrix.

    Pure validation -- raises ValueError on any violation, returns nothing.
    Normalizing similarity (upper-casing it) is the caller's job: a function
    named "validate" shouldn't also silently compute and hand back a derived
    value.
    """
    if index_type not in _VALID_INDEX_TYPES:
        raise ValueError(
            f"index_type must be one of {sorted(_VALID_INDEX_TYPES)}, got {index_type!r}"
        )

    if index_type == "scalar":
        _reject_vector_only_params(
            vector_field,
            dimension,
            similarity,
            description,
            include,
            vector_index_position,
        )
        if not keys:
            raise ValueError(
                "keys is required and must be non-empty for index_type='scalar'"
            )
        return

    if index_type == "hyperscale_vector":
        if keys:
            raise ValueError(
                "keys is not valid for index_type='hyperscale_vector' -- Hyperscale "
                "takes a single vector_field plus optional include"
            )
        if vector_index_position is not None:
            raise ValueError(
                "vector_index_position is only valid for index_type="
                "'composite_vector' -- Hyperscale has exactly one key"
            )
    else:  # composite_vector
        if include:
            raise ValueError(
                "include is Hyperscale-only -- a Composite vector index has no "
                "INCLUDE clause; add scalar fields to keys instead"
            )
        if vector_index_position is not None and not (
            0 <= vector_index_position <= len(keys or [])
        ):
            raise ValueError(
                f"vector_index_position ({vector_index_position}) must be between "
                f"0 and len(keys) ({len(keys or [])}), inclusive"
            )

    _require_vector_fields(index_type, vector_field, dimension, similarity)


def _build_with_clause(
    deferred: bool,
    num_replicas: int | None,
    dimension: int | None,
    similarity: str | None,
    description: str | None,
    with_options: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the WITH-clause options dict.

    ``defer_build`` is always set explicitly. SQL++'s own default is
    ``false`` (build immediately), but this tool's own default is
    ``deferred=True`` to match the legacy ``create_index`` tool's behavior --
    if ``WITH`` were only emitted when some other option were set, a plain
    scalar call with no extra options would silently build immediately,
    breaking that default.
    """
    with_clause: dict[str, Any] = {"defer_build": deferred}
    if num_replicas is not None:
        with_clause["num_replicas"] = num_replicas
    if dimension is not None:
        with_clause["dimension"] = dimension
    if similarity is not None:
        with_clause["similarity"] = similarity
    if description is not None:
        with_clause["description"] = description
    if with_options:
        overlap = _RESERVED_WITH_KEYS & with_options.keys()
        if overlap:
            raise ValueError(
                f"with_options cannot override {sorted(overlap)} -- use the "
                "dedicated dimension/similarity/description/num_replicas/deferred "
                "parameters instead"
            )
        with_clause.update(with_options)
    return with_clause


def _build_statement(
    index_type: str,
    index_name: str,
    collection_name: str,
    keys: list[str] | None,
    vector_field: str | None,
    include: list[str] | None,
    vector_index_position: int | None,
    condition: str | None,
    ignore_if_exists: bool,
    with_clause: dict[str, Any],
) -> str:
    """Build the CREATE INDEX / CREATE VECTOR INDEX SQL++ DDL statement.

    keys/condition stay raw, uninterpreted SQL++ -- the same trust model
    already shipped in the legacy create_index tool (backtick-wrapping an
    expression like "created_at DESC" would break it). index_name/
    collection_name/field paths are the only pieces quoted, via
    safe_ident/safe_field_path.
    """
    exists_clause = " IF NOT EXISTS" if ignore_if_exists else ""
    where_clause = f" WHERE {condition}" if condition else ""
    with_json = json.dumps(with_clause)

    if index_type == "scalar":
        key_clause = ", ".join(keys)  # type: ignore[arg-type]
        return (
            f"CREATE INDEX {safe_ident(index_name)}{exists_clause} "
            f"ON {safe_ident(collection_name)} ({key_clause})"
            f"{where_clause} WITH {with_json}"
        )

    if index_type == "hyperscale_vector":
        include_clause = (
            f" INCLUDE ({', '.join(safe_field_path(f) for f in include)})"
            if include
            else ""
        )
        return (
            f"CREATE VECTOR INDEX {safe_ident(index_name)}{exists_clause} "
            f"ON {safe_ident(collection_name)} "
            f"({safe_field_path(vector_field)} VECTOR){include_clause}"
            f"{where_clause} WITH {with_json}"
        )

    # composite_vector: vector key interleaved among the scalar keys at
    # vector_index_position (defaulting to the end).
    scalar_keys = list(keys or [])
    position = (
        len(scalar_keys) if vector_index_position is None else vector_index_position
    )
    full_keys = [
        *scalar_keys[:position],
        f"{safe_field_path(vector_field)} VECTOR",
        *scalar_keys[position:],
    ]
    key_clause = ", ".join(full_keys)
    return (
        f"CREATE INDEX {safe_ident(index_name)}{exists_clause} "
        f"ON {safe_ident(collection_name)} ({key_clause})"
        f"{where_clause} USING GSI WITH {with_json}"
    )


def create_query_index(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    index_name: str,
    index_type: str,
    keys: list[str] | None = None,
    vector_field: str | None = None,
    dimension: int | None = None,
    similarity: str | None = None,
    description: str | None = None,
    include: list[str] | None = None,
    vector_index_position: int | None = None,
    with_options: dict[str, Any] | None = None,
    condition: str | None = None,
    num_replicas: int | None = None,
    deferred: bool = True,
    ignore_if_exists: bool = False,
) -> dict[str, Any]:
    """Create a GSI index -- scalar, Hyperscale vector, or Composite vector -- via SQL++.

    Builds and executes the raw SQL++ DDL for a GSI index -- a plain scalar
    secondary index, a Hyperscale Vector Index, or a Composite Vector Index --
    selected via index_type. Neither vector index shape has a Couchbase SDK
    management API, so all three shapes are created the same way here: by
    constructing and running a CREATE INDEX / CREATE VECTOR INDEX statement
    directly.

    index_type selects the shape and which other parameters apply:

    - "scalar": an ordinary secondary index. Requires keys (the field(s)/
      expression(s) to index, e.g. ["type", "created_at DESC"], passed through
      to the DDL uninterpreted). vector_field/dimension/similarity/description/
      include/vector_index_position are invalid here.
    - "hyperscale_vector": a Hyperscale Vector Index -- a single vector key,
      optimized for pure similarity search at scale, Couchbase Server 8.0+.
      Requires vector_field, dimension, and similarity. include optionally
      lists scalar fields to carry alongside the vector key (for projection,
      not filtering acceleration) -- INCLUDE only exists on Hyperscale. keys
      and vector_index_position are invalid here.
    - "composite_vector": a Composite Vector Index -- scalar keys plus one
      vector key in the same index, best when a scalar predicate is selective
      and filters most of the corpus on nearly every query. Requires
      vector_field, dimension, and similarity; keys is optional (additional
      scalar keys) and vector_index_position (0-based) controls where the
      vector key is inserted among them, defaulting to the end. include is
      invalid here (Hyperscale-only).

    dimension must exactly match the length of every vector you intend to index,
    and values must be 32-bit floats -- Couchbase does not infer or default this
    (required, no default; confirmed against docs.couchbase.com's CREATE VECTOR
    INDEX reference). Determine it from your embedding model's known output
    size, or by inspecting a real document (e.g. via get_document_by_id) -- do
    not guess. A document whose vector doesn't match the index's dimension or
    type is NOT a build error: per Couchbase's documentation, the vector is
    treated as NULL and that document is silently excluded from the index.
    There is no per-document error and no index-build failure -- the only
    trace is an internal indexer error counter this tool cannot surface.
    Validate your data yourself before calling this tool.

    similarity is required for both vector shapes, deliberately with no default
    (unlike the raw DDL, which defaults to L2_SQUARED if omitted) -- it must
    match whatever distance_metric you will later query with
    (run_vector_search), and Couchbase's own default is usually not what an
    embedding model wants. Valid values (case-insensitive): EUCLIDEAN_SQUARED,
    EUCLIDEAN, L2_SQUARED, L2, COSINE, DOT. Note 'DOT_PRODUCT' is the Search
    (FTS) service's spelling, not GSI's -- it is rejected here with a message
    pointing at 'DOT' instead, a real and common point of confusion between
    the two services.

    description is the optional quantization/clustering spec (defaults to
    Couchbase's own "IVF,SQ8" if omitted) -- not validated here beyond being a
    string, since additional quantization schemes (e.g. RaBitQ) exist beyond
    the commonly documented pattern; an invalid value surfaces as the Index
    service's own error. with_options passes through any additional WITH-clause
    fields (e.g. scan_nprobes, train_list, persist_full_vector) verbatim -- it
    cannot override dimension, similarity, description, num_replicas, or
    deferred, which this tool already manages explicitly.

    condition is an optional WHERE clause for a partial index -- this works for
    the vector shapes too, not just scalar; the planner only selects the index
    for a query whose own predicate implies condition, so a query tool's
    filter (e.g. run_vector_search's where) must match or imply it, or the
    index is silently skipped in favor of a full scan rather than erroring.
    num_replicas optionally sets index replica count. By default the index is created
    deferred (not built) -- call build_index afterward, then list_indexes to
    confirm it reaches 'online'. Pass ignore_if_exists=True to avoid an error
    when an index with this name already exists.

    Returns {"success": True, "index_name", "index_type", "keyspace", "deferred",
    "statement" (the exact SQL++ DDL executed, for debugging/auditing)} on
    success, with a "next_step" hint when deferred is True. On failure returns
    {"success": False, "error": ...}.
    """
    keyspace = format_keyspace(bucket_name, scope_name, collection_name)
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)

    try:
        _validate_params(
            index_type,
            keys,
            vector_field,
            dimension,
            similarity,
            description,
            include,
            vector_index_position,
        )
        is_vector = index_type != "scalar"
        # Normalized here, not inside _validate_params -- a validation
        # function should only raise or not, not also hand back a derived
        # value.
        normalized_similarity = similarity.upper() if is_vector else None
        with_clause = _build_with_clause(
            deferred=deferred,
            num_replicas=num_replicas,
            dimension=dimension if is_vector else None,
            similarity=normalized_similarity,
            description=description if is_vector else None,
            with_options=with_options,
        )
        statement = _build_statement(
            index_type,
            index_name,
            collection_name,
            keys,
            vector_field,
            include,
            vector_index_position,
            condition,
            ignore_if_exists,
            with_clause,
        )

        logger.debug(
            f"Creating {index_type} index {index_name!r} on {keyspace}: {statement}"
        )
        result = bucket.scope(scope_name).query(statement)
        list(result)  # force execution -- query() is lazily streamed

        logger.info(
            f"Created {index_type} index {index_name!r} on {keyspace} "
            f"(deferred={deferred})"
        )
        response = tool_success(
            index_name=index_name,
            index_type=index_type,
            keyspace=keyspace,
            deferred=deferred,
            statement=statement,
        )
        if deferred:
            response["next_step"] = (
                f"Index '{index_name}' was created deferred and is NOT yet usable. "
                "Call build_index to build it, then list_indexes to confirm it "
                "reaches 'online'."
            )
        return response
    except ValueError as e:
        # Input-shape mistakes an LLM caller makes routinely (wrong
        # index_type, missing dimension/similarity, a param that doesn't
        # apply to the chosen shape) -- expected, not exceptional, so no
        # traceback noise.
        logger.warning(f"Rejected {index_type} index {index_name!r} on {keyspace}: {e}")
        return tool_error(
            e, index_name=index_name, keyspace=keyspace, index_type=index_type
        )
    except Exception as e:
        logger.error(
            f"Error creating {index_type} index {index_name!r} on {keyspace}: {e}",
            exc_info=True,
        )
        return tool_error(
            e, index_name=index_name, keyspace=keyspace, index_type=index_type
        )
