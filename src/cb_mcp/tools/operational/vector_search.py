"""Vector search tools.

This module covers the vector search feature `fts.py` explicitly disclaims:
its own module docstring says vector queries "require the SDK's SearchRequest
+ VectorSearch combination, which these tools do not build ... a separate
feature." That separate feature lives here, as two tools built on two
genuinely different Couchbase mechanisms:

- run_vector_search: Couchbase 8.0's GSI vector indexes (Composite Vector
  Index / Hyperscale Vector Index), queried via plain SQL++'s
  APPROX_VECTOR_DISTANCE() in an ORDER BY. GSI selects the index
  automatically from the vector field referenced in the query, the same way
  ordinary scalar GSI queries do -- no index name is ever passed.
- run_search_vector_search: the FTS/Search service's VectorQuery/VectorSearch/
  SearchRequest API, which always targets a *named* Search index and is the
  only mechanism that supports true hybrid search (a scalar FTS query and a
  vector query combined into one ranked result set).

Both tools turn query text into a vector via the pluggable embedding-provider
abstraction in utils.operational.embeddings, selected by the EMBEDDING_*
settings (see registry.PROVIDER_CONFIG_DOCS for exactly what each provider
needs). Embedding config is entirely optional at server startup -- these
tools fail with an actionable tool_error at call time if it's missing, they
are not hidden from tool discovery.

Error handling deliberately widens fts.py's convention: there, only
get_cluster_connection's exception propagates and connect_to_bucket's
failures are folded into the tool_error envelope. Here, both
get_cluster_connection and connect_to_bucket are treated as the same class
of problem -- can't reach the thing we were asked to search -- and both
propagate uncaught. Everything past that point (missing embedding config, an
embedding request failure, a malformed query, an index that doesn't exist)
is a problem *with the request*, not the connection, and is reported back
via tool_success/tool_error instead of raising.
"""

import logging
from typing import Any

from couchbase.options import SearchOptions
from couchbase.search import RawQuery, SearchRequest
from couchbase.vector_search import VectorQuery, VectorSearch
from fastmcp import Context

from ...servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ...utils.config import get_settings
from ...utils.operational.connection import connect_to_bucket
from ...utils.operational.context import get_cluster_connection
from ...utils.operational.embeddings import EmbeddingRequest, resolve_embedding_provider
from ...utils.operational.index_utils import resolve_cluster_major_version
from ...utils.responses import tool_error, tool_success
from ...utils.sqlpp import quote_literal, safe_ident

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.tools.vector_search")


def _embed_query(ctx: Context, query_text: str) -> tuple[list[float], dict[str, Any]]:
    """Resolve the configured provider and embed query_text.

    Returns (vector, info) rather than an EmbeddingResult so callers can drop
    `info` straight into their tool_success payload without re-deriving it.
    Raises EmbeddingConfigError / whatever the provider's embed() raises --
    both callers wrap this in their own try/except and turn it into
    tool_error, matching every other failure mode in this module.
    """
    settings = get_settings(ctx)
    provider = resolve_embedding_provider(settings)
    result = provider.embed(
        EmbeddingRequest(text=query_text, model=settings.get("embedding_model") or "")
    )
    return result.vector, {
        "embedding_model": result.model,
        "embedding_dimensions": result.dimensions,
    }


def _best_effort_cluster_major_version(cluster: Any) -> int | None:
    """Detect the cluster's major version for run_vector_search's advisory
    "warning" field (see its docstring). Purely informational -- a pre-8.0
    cluster already fails APPROX_VECTOR_DISTANCE with its own clear "unknown
    function" error, so version detection failing here must never fail the
    search itself; swallowed to None rather than raised or logged as an
    error (resolve_cluster_major_version logs its own reasoning at INFO).
    """
    try:
        return resolve_cluster_major_version(cluster)
    except Exception:
        return None


def run_vector_search(
    ctx: Context,
    bucket_name: str,
    scope_name: str,
    collection_name: str,
    vector_field: str,
    query_text: str,
    distance_metric: str = "cosine",
    limit: int = 10,
    where: str | None = None,
    select_fields: list[str] | None = None,
    num_probes: int | None = None,
    rerank: int | None = None,
    top_n_scan: int | None = None,
) -> dict[str, Any]:
    """Embed a query and run a vector similarity search against a GSI vector index.

    Embedding: query_text is embedded using the model configured via
    EMBEDDING_PROVIDER/EMBEDDING_MODEL/EMBEDDING_API_KEY/EMBEDDING_ENDPOINT
    (plus EMBEDDING_AWS_* for provider=bedrock). If no provider is configured,
    this tool returns {"success": False, "error": ...} explaining what to set
    -- it is not hidden from tool discovery when unconfigured.

    Index selection: this tool queries a Couchbase 8.0+ GSI vector index
    (Composite Vector Index or Hyperscale Vector Index) via SQL++'s
    APPROX_VECTOR_DISTANCE(). Unlike Search-service vector search, GSI selects
    the index automatically from the vector field referenced in the query --
    there is no index_name parameter, and none is needed.

    num_probes/rerank/top_n_scan are Hyperscale Vector Index tuning
    parameters (centroids to probe, rerank count, top-N scan). Pass all three
    together or none -- a Composite Vector Index doesn't use them, and this
    tool has no way to tell which index type will actually serve the query.

    where is an optional raw SQL++ boolean expression (e.g. "b.status =
    'active'") appended to prefilter candidates before the vector ordering --
    this tool only ever emits a SELECT, so, unlike run_sql_plus_plus_query,
    there is no write-guard for it to bypass.

    select_fields limits the projected columns; omit it to return full
    documents (b.*). limit defaults to 10, matching the other query/search
    tools in this server.

    Returns {"success": True, "hits": [{"id", "distance", ...selected
    fields...}], "total_hits", "cluster_major_version", and a "warning" key
    only when the detected cluster major version is below 8 (informational --
    a pre-8.0 cluster fails the query naturally with its own "unknown
    function" error rather than being blocked here)} or
    {"success": False, "error": ...}.
    """

    if (num_probes is None) != (rerank is None) or (num_probes is None) != (
        top_n_scan is None
    ):
        return tool_error(
            "num_probes, rerank, and top_n_scan must be provided together, or all omitted"
        )

    # Connection problems -- can't reach the cluster, or this bucket doesn't
    # exist / isn't reachable -- propagate uncaught rather than becoming a
    # tool_error; see the module docstring.
    cluster = get_cluster_connection(ctx)
    bucket = connect_to_bucket(cluster, bucket_name)
    logger.debug(
        f"run_vector_search on {bucket_name}.{scope_name}.{collection_name} "
        f"(vector_field={vector_field!r}, distance_metric={distance_metric!r}, "
        f"limit={limit}, hyperscale_tuning={num_probes is not None})"
    )

    try:
        vector, embedding_info = _embed_query(ctx, query_text)

        distance_args = f"b.{safe_ident(vector_field)}, $query_vector, {quote_literal(distance_metric)}"
        if num_probes is not None:
            distance_args += f", {int(num_probes)}, {int(rerank)}, {int(top_n_scan)}"
        distance_expr = f"APPROX_VECTOR_DISTANCE({distance_args})"

        projection = (
            "b.*"
            if not select_fields
            else ", ".join(f"b.{safe_ident(field)}" for field in select_fields)
        )
        # Identifiers are backtick-quoted (safe_ident) and the vector itself
        # is a bound named parameter, not interpolated -- same noqa: S608
        # precedent as operational_insights/metadata.py's schema-infer query.
        query = (
            f"SELECT META(b).id AS id, {distance_expr} AS distance, {projection} "  # noqa: S608
            f"FROM {safe_ident(collection_name)} AS b "
            + (f"WHERE {where} " if where else "")
            + f"ORDER BY {distance_expr} LIMIT {int(limit)}"
        )

        result = bucket.scope(scope_name).query(
            query, named_parameters={"query_vector": vector}
        )
        hits = list(result)
        major = _best_effort_cluster_major_version(cluster)

        response = tool_success(
            bucket_name=bucket_name,
            scope_name=scope_name,
            collection_name=collection_name,
            vector_field=vector_field,
            distance_metric=distance_metric,
            limit=limit,
            total_hits=len(hits),
            hits=hits,
            cluster_major_version=major,
            **embedding_info,
        )
        if major is not None and major < 8:
            response["warning"] = (
                f"Detected cluster major version {major}; GSI vector indexes "
                "require Couchbase Server 8.0+. The query above will fail if "
                "the cluster doesn't actually support APPROX_VECTOR_DISTANCE."
            )
        logger.info(
            f"run_vector_search on {bucket_name}.{scope_name}.{collection_name} "
            f"returned {len(hits)} hit(s)"
        )
        return response
    except Exception as e:
        logger.error(f"Error running vector search: {e}", exc_info=True)
        return tool_error(
            e,
            bucket_name=bucket_name,
            scope_name=scope_name,
            collection_name=collection_name,
        )


def run_search_vector_search(
    ctx: Context,
    index_name: str,
    vector_field: str,
    vector_query_text: str,
    scalar_query: dict[str, Any] | None = None,
    bucket_name: str | None = None,
    scope_name: str | None = None,
    num_candidates: int = 10,
    limit: int | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Run the Search service's vector search, including hybrid (vector + scalar) search.

    Embedding: vector_query_text is embedded the same way as
    run_vector_search's query_text -- see that tool's docstring for
    EMBEDDING_* configuration.

    Unlike run_vector_search's GSI-backed search, this tool always targets a
    *named* Search (FTS) index -- index_name is required. Pass both
    bucket_name and scope_name together to query a scope-level (scoped)
    index, or omit both to query a cluster-level ("legacy") index. Confirm
    the index's exact name/location first with list_fts_indexes if unsure --
    this tool does not pre-validate index_name; a wrong or non-vector index
    name fails naturally at the Search service with its own error.

    scalar_query is optional and, when given, makes this a genuinely hybrid
    search: it is the same raw FTS query JSON body run_fts_query accepts
    (e.g. {"match": "jacket", "field": "description"}) -- both the vector
    query and this scalar query are sent to the Search service together, and
    it ranks/combines their hits server-side. Omit it for a vector-only
    search through this tool (equivalent in intent to run_vector_search, but
    against a named Search index instead of a GSI vector index).

    num_candidates bounds how many nearest-neighbor candidates the vector
    query considers. limit defaults to 10 (matching run_fts_query). fields
    requests specific stored index fields per hit.

    This tool does not fetch full document bodies -- a Search hit carries
    whatever fields the index stores (row.fields), the same as run_fts_query,
    not a KV-fetched document (a hit has no reliable per-row collection name
    to fetch against). Follow up with get_document_by_id for any hit you need
    the full document for.

    Returns {"success": True, "index_name", "vector_field", "num_candidates",
    "limit", "total_hits", "hits": [{"id", "score", "fields"}], "is_hybrid"}
    or {"success": False, "error": ...}.
    """

    if (bucket_name is None) != (scope_name is None):
        return tool_error(
            "bucket_name and scope_name must be provided together, or omitted together"
        )

    # Connection problems -- can't reach the cluster, or this bucket doesn't
    # exist / isn't reachable -- propagate uncaught rather than becoming a
    # tool_error; see the module docstring.
    cluster = get_cluster_connection(ctx)
    bucket = (
        connect_to_bucket(cluster, bucket_name) if bucket_name and scope_name else None
    )
    logger.debug(
        f"run_search_vector_search on {index_name!r} (vector_field={vector_field!r}, "
        f"num_candidates={num_candidates}, hybrid={scalar_query is not None})"
    )

    try:
        vector, embedding_info = _embed_query(ctx, vector_query_text)

        vq = VectorQuery(vector_field, vector, num_candidates=num_candidates)
        request = SearchRequest.create(VectorSearch.from_vector_query(vq))
        if scalar_query:
            request = request.with_search_query(RawQuery(scalar_query))

        applied_limit = limit if limit is not None else 10
        options = SearchOptions(limit=applied_limit, fields=fields)

        if bucket is not None:
            result = bucket.scope(scope_name).search(index_name, request, options)
        else:
            result = cluster.search(index_name, request, options)

        hits = [
            {"id": row.id, "score": row.score, "fields": row.fields}
            for row in result.rows()
        ]

        logger.info(
            f"run_search_vector_search on {index_name!r} (hybrid={scalar_query is not None}) "
            f"returned {len(hits)} hit(s)"
        )
        return tool_success(
            index_name=index_name,
            vector_field=vector_field,
            num_candidates=num_candidates,
            limit=applied_limit,
            total_hits=len(hits),
            hits=hits,
            is_hybrid=scalar_query is not None,
            **embedding_info,
        )
    except Exception as e:
        logger.error(
            f"Error running Search vector search on {index_name!r}: {e}", exc_info=True
        )
        return tool_error(e, index_name=index_name)
