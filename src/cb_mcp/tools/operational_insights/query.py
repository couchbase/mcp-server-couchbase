"""Query execution tools for Operational Insights.

``run_query_sync`` can carry DDL/DML, so — unlike the metadata tools in this
package — it follows the operational server's write-tool convention: catch
Exception, log, and return a ``{"success": False, "error": ...}`` envelope
instead of raising.

``explain_query`` follows the same envelope convention.

The Server Async Request API tools (``run_query_async``,
``get_async_query_results``, ``discard_async_query_results``,
``cancel_async_query``) expose the SDK's handle-based flow for long-running
queries: start -> check/fetch -> discard, or cancel.
``get_async_query_results`` does double duty as the readiness check, so there
is no separate status tool.

Design notes (kept out of the tool docstrings, which are sent to the model as
tool descriptions and are deliberately short):

* The live SDK handle objects hold an HTTP client and a thread pool, so they
  cannot be serialized to the client. They stay in a server-side
  ``QueryResultsRegistry``, referenced by an opaque ``query_handle`` token. See
  ``utils/operational_insights/handle_registry.py`` for the design and its
  single-process caveat.
* Fetching results does NOT free them: the server serves the same buffers on
  repeated fetches. So a token stays valid after ``get_async_query_results``;
  only discard and cancel evict it. Callers that never discard leave buffers
  allocated until the server times them out.
* Tools are plain ``def`` (not ``async def``): the SDK's handle calls are
  blocking, so they run on FastMCP's thread pool rather than blocking the
  event loop inside a coroutine.
"""

import logging
import re
from typing import Any

from couchbase_operational_insights.options import QueryOptions
from fastmcp import Context
from fastmcp.server.dependencies import get_access_token

from ...servers.operational_insights.constants import (
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)
from ...utils.constants import SCOPE_WRITE
from ...utils.operational_insights.context import get_oi_cluster, get_oi_handle_registry
from ...utils.query_limits import (
    collect_rows_within_budget,
    max_query_result_size_for,
)
from ...utils.responses import tool_error, tool_success
from ...utils.sqlpp import quote_literal, safe_ident

logger = logging.getLogger(f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.tools.query")


_LEADING_WHITESPACE_OR_COMMENT = re.compile(r"\s+|--[^\n]*\n?|/\*.*?\*/", re.DOTALL)


def _strip_leading_comments(statement: str) -> str:
    """Strip leading whitespace and SQL++ comments (``--`` and ``/* */``).

    Comments and whitespace can be freely mixed and repeated before the
    first real token (e.g. ``/* a */ -- b\nCOPY ...``), so this loops rather
    than matching once. A single regex with a repeated group would also
    match this, but Python's backtracking lets a greedy ``--[^\n]*`` shrink
    so a trailing "COPY " gets read as living *outside* the comment (e.g.
    ``-- COPY ds TO ...`` with no newline) -- this loop advances a match
    position instead of backtracking, so each comment is consumed in full.
    """
    pos = 0
    while True:
        match = _LEADING_WHITESPACE_OR_COMMENT.match(statement, pos)
        if match is None:
            return statement[pos:]
        pos = match.end()


def _is_copy_to_statement(statement: str) -> bool:
    """True for a ``COPY ... TO`` statement.

    Covers both forms — export to external object storage and export to a
    KV collection — since both share the same leading keyword; there is no
    need to parse the rest of the grammar to tell them apart here. Leading
    comments are stripped first, since a comment before the keyword (e.g.
    ``-- export\nCOPY ds TO ...``) would otherwise let the statement dodge
    this check while still executing as COPY ... TO.
    """
    normalized = _strip_leading_comments(statement).upper()
    return re.match(r"^COPY\s", normalized) is not None


#: Output formats the EA server accepts in ``COPY ... TO ... WITH {"format": ...}``.
#: Validated client-side so a typo is a clear tool error naming the valid set,
#: rather than a server error the model has to decode.
#:
#: The set comes from the server itself, which answers an unsupported format
#: with "Supported formats: [csv, json, parquet]" — so ``tsv`` does not exist.
#: ``csv`` is deliberately excluded even though the server lists it: a CSV
#: export additionally requires a ``TYPE(...)`` clause naming the output schema
#: ("TYPE/AS Expression is required for csv format"), which cannot be inferred
#: from an arbitrary SELECT. Offering it would fail compilation on every call,
#: whereas ``json`` and ``parquet`` carry their own schema and work from any
#: statement.
COPY_TO_FORMATS = ("json", "parquet")
DEFAULT_COPY_TO_FORMAT = "json"


class CopyToError(ValueError):
    """An export was requested with an incomplete or invalid destination.

    Its own type so the callers can turn it into a ``tool_error`` envelope
    without catching — and swallowing — genuine SDK failures from the same
    ``try`` block.
    """


def build_copy_to_statement(
    statement: str,
    *,
    link: str,
    bucket: str,
    path: str,
    output_format: str | None = None,
) -> str:
    """Wrap ``statement`` in a ``COPY ... TO`` that exports its rows.

    Produces::

        COPY ( <statement> ) AS t
        TO `<bucket>` AT <link>
        PATH("<path>")
        WITH {"format": "<format>"}

    Three different quoting rules apply, which is the whole reason this is a
    function rather than an f-string at each call site:

    * ``bucket`` is an identifier -> backtick-quoted via ``safe_ident``.
    * ``path`` is a string literal -> double-quoted via ``quote_literal``.
    * ``link`` is a *raw* identifier token. The EA grammar takes it bare after
      ``AT``, so it cannot be backtick-quoted; it is validated against a
      conservative character class instead, since an unquotable value
      interpolated into a statement is an injection point.
    """
    if not link or not link.strip():
        raise CopyToError("copy_to_link is required to export results.")
    if not bucket or not bucket.strip():
        raise CopyToError("copy_to_bucket is required to export results.")
    if not path or not path.strip():
        raise CopyToError("copy_to_path is required to export results.")

    link = link.strip()
    # The link name is interpolated unquoted (the grammar accepts no quoting
    # after AT), so restrict it to characters that cannot terminate the token.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-]*", link):
        raise CopyToError(
            f"copy_to_link {link!r} is not a valid link name: expected letters, "
            f"digits, underscore or hyphen, starting with a letter or underscore."
        )

    fmt = (output_format or DEFAULT_COPY_TO_FORMAT).strip().lower()
    if fmt not in COPY_TO_FORMATS:
        raise CopyToError(
            f"copy_to_format {output_format!r} is not supported; "
            f"expected one of {', '.join(COPY_TO_FORMATS)}."
        )

    inner = statement.strip().rstrip(";")
    return (
        f"COPY (\n{inner}\n) AS t\n"
        f"TO {safe_ident(bucket.strip())} AT {link}\n"
        f"PATH({quote_literal(path.strip())})\n"
        f'WITH {{"format": {quote_literal(fmt)}}}'
    )


def _resolve_copy_to(
    statement: str,
    *,
    link: str | None,
    bucket: str | None,
    path: str | None,
    output_format: str | None,
) -> tuple[str, dict[str, Any] | None]:
    """Decide whether this call is an export, and build the statement if so.

    Returns ``(statement_to_run, destination_or_None)``. ``destination`` is
    ``None`` for an ordinary query, and otherwise the JSON-safe description of
    where the rows went, echoed back in the tool's envelope.

    Partial destinations are rejected rather than ignored. Dropping a
    half-specified export and silently running the plain query would return
    rows the caller never asked for and write nothing — the opposite of the
    request, and invisible unless they noticed the missing file.
    """
    requested = [p for p in (link, bucket, path) if p and p.strip()]
    if not requested:
        if output_format:
            raise CopyToError(
                "copy_to_format was given without a destination; "
                "copy_to_link, copy_to_bucket and copy_to_path are all "
                "required to export."
            )
        return statement, None

    copy_statement = build_copy_to_statement(
        statement,
        link=link,  # type: ignore[arg-type]  # validated inside
        bucket=bucket,  # type: ignore[arg-type]
        path=path,  # type: ignore[arg-type]
        output_format=output_format,
    )
    return copy_statement, {
        "link": link.strip(),  # type: ignore[union-attr]
        "bucket": bucket.strip(),  # type: ignore[union-attr]
        "path": path.strip(),  # type: ignore[union-attr]
        "format": (output_format or DEFAULT_COPY_TO_FORMAT).strip().lower(),
    }


def run_query_sync(
    ctx: Context,
    statement: str,
    copy_to_link: str | None = None,
    copy_to_bucket: str | None = None,
    copy_to_path: str | None = None,
    copy_to_format: str | None = None,
) -> dict[str, Any]:
    """Run a SQL++ statement and return its result rows, or export them to object storage.

    Can carry SELECT, DML, or DDL statements. Rows are streamed from the
    server and collected up to a configured byte budget; a result that would
    exceed it is cut short and reported with truncated: true.

    To send a large result straight to object storage instead of returning it,
    pass copy_to_link, copy_to_bucket and copy_to_path. The statement is then
    wrapped in COPY ... TO and the rows are written to the external bucket; the
    tool returns a confirmation with no rows, so a result too large to read
    inline costs nothing in context. Use this when a query would otherwise be
    truncated and the full data is needed.

    An export here waits for the whole upload to finish before returning, so
    the call takes as long as the copy does. For a large export prefer
    run_query_async, which returns a handle once the query is submitted and
    lets you poll for completion instead of holding the request open.

    When the server is in read-only mode, or the caller's token lacks the
    write scope, the statement is executed with ``QueryOptions(readonly=True)``
    so the Operational Insights server itself rejects DML/DDL — there is no
    client-side SQL++ parser here (unlike the operational server's
    ``run_sql_plus_plus_query``), so enforcement is otherwise entirely
    server-side.

    One statement needs an extra, client-side check on top of that:
    ``COPY ... TO`` is classified as read-only by the server itself (it
    doesn't modify anything already stored), so ``readonly=True`` alone does
    not block it — but it writes its result to external object storage or a
    KV collection, which this server's read-only guarantee still treats as a
    write. Blocked here via a regex on the leading keyword rather than a
    full grammar parser, since read-only mode is the only thing that cares
    about the distinction.

    Args:
        statement: The SQL++ statement to execute.
        copy_to_link: Name of an existing external link (e.g. an S3 link
            created with CREATE LINK). Required to export.
        copy_to_bucket: Destination bucket in the external store. Required
            to export.
        copy_to_path: Path prefix within that bucket, e.g. "exports/run1".
            Required to export.
        copy_to_format: Output format — json (default) or parquet.

    Returns {"success": True, "rows": [...], "row_count": N,
    "truncated": bool} on success, or {"success": False, "error": "..."} on
    failure. When truncated is true the remaining rows were not read and
    cannot be retrieved by calling again — narrow the statement, or export it
    with the copy_to_* arguments.

    When exporting, returns {"success": True, "exported": True,
    "destination": {...}} and no rows — the data is in object storage.
    """
    cluster = get_oi_cluster(ctx)

    app_context = ctx.request_context.lifespan_context
    read_only_mode = app_context.read_only_mode

    # Mirrors run_sql_plus_plus_query's scope-gap closure: a token holding
    # only SCOPE_READ must not be able to mutate through this tool, since the
    # per-tool scope wrapper classifies it as a read tool (see
    # tools/operational_insights/__init__.py TOOL_SET). When no token is
    # present (stdio / OAuth disabled), lacks_write_scope is False, so the
    # historical read_only_mode-only behavior is preserved.
    token = get_access_token()
    lacks_write_scope = token is not None and SCOPE_WRITE not in (token.scopes or [])
    enforce_readonly = read_only_mode or lacks_write_scope

    try:
        statement_to_run, destination = _resolve_copy_to(
            statement,
            link=copy_to_link,
            bucket=copy_to_bucket,
            path=copy_to_path,
            output_format=copy_to_format,
        )
    except CopyToError as e:
        logger.debug(f"Rejecting export request: {e}")
        return tool_error(e, statement=statement)

    # Checks the statement actually being sent, so an export built from the
    # copy_to_* arguments is gated exactly like one the caller wrote by hand.
    if enforce_readonly and _is_copy_to_statement(statement_to_run):
        logger.debug("Blocking COPY ... TO statement under read-only mode")
        return tool_error(
            "COPY ... TO is blocked under read-only mode: the server itself "
            "classifies it as read-only, but it writes its result to "
            "external storage or a KV collection.",
            statement=statement,
        )

    try:
        logger.debug(
            f"Running SQL++ statement synchronously "
            f"(readonly={enforce_readonly}, export={destination is not None})"
        )
        result = (
            cluster.execute_query(statement_to_run, QueryOptions(readonly=True))
            if enforce_readonly
            else cluster.execute_query(statement_to_run)
        )
        if destination is not None:
            # A COPY ... TO returns no result rows — the data went to the
            # external store. Draining the (empty) stream anyway keeps the
            # SDK's response fully consumed before the handle is dropped.
            result.get_all_rows()
            logger.info(
                f"Exported query results to {destination['bucket']}/"
                f"{destination['path']} via link {destination['link']}"
            )
            return tool_success(
                exported=True,
                destination=destination,
                message=(
                    f"Results were written to "
                    f"{destination['bucket']}/{destination['path']} as "
                    f"{destination['format']}. No rows are returned for an "
                    f"export; read them from object storage."
                ),
            )

        # result.rows() is the SDK's lazy BlockingIterator; get_all_rows()
        # would be list() over the same stream, buffering the whole result.
        bounded = collect_rows_within_budget(
            result.rows(),
            limit_bytes=max_query_result_size_for(ctx),
            service="operational-insights sync",
        )
        logger.info(
            f"Query returned {bounded.row_count} row(s) (truncated={bounded.truncated})"
        )
        return tool_success(
            rows=bounded.rows,
            row_count=bounded.row_count,
            **bounded.as_envelope_fields(),
        )
    except Exception as e:
        logger.error(f"Error running query: {e}", exc_info=True)
        return tool_error(e, statement=statement)


def explain_query(ctx: Context, statement: str) -> dict[str, Any]:
    """Generate the query plan for a SQL++ statement using EXPLAIN, without executing it.

    Whether the plan is cost-based or rule-based depends on whether the
    optimizer has collected samples for the target collection(s) — this tool
    just returns whatever plan the optimizer produces.

    Pass the statement without an EXPLAIN keyword; it is added automatically.

    Deliberately does not pass QueryOptions(readonly=True): EXPLAIN does not
    execute the statement it plans, and a readonly session may refuse to
    even plan a DDL/DML statement.

    Returns {"success": True, "plan": [...]} on success, or
    {"success": False, "error": "..."} on failure.
    """
    cluster = get_oi_cluster(ctx)
    try:
        logger.debug("Running EXPLAIN for SQL++ statement")
        result = cluster.execute_query(f"EXPLAIN {statement}")
        rows = result.get_all_rows()
        logger.info(f"EXPLAIN returned {len(rows)} row(s)")
        return tool_success(plan=rows)
    except Exception as e:
        logger.error(f"Error running EXPLAIN: {e}", exc_info=True)
        return tool_error(e, statement=statement)


def _extract_metadata(result: Any, query_handle: str) -> dict[str, Any]:
    """Convert a query result's metadata into a JSON-serializable dict.

    The SDK exposes metadata as objects behind accessor methods
    (``QueryMetadata`` / ``QueryMetrics``), none of which serialize, so each
    value is read out by hand. Timing metrics come back as ``timedelta``, which
    is not JSON-safe either, and are emitted as float milliseconds.

    Metadata is a nicety, not the payload: a field the server did not send
    should not fail an otherwise successful fetch, so every read is
    independently guarded and whatever was gathered is returned.
    """

    def _read(fn: Any, label: str) -> Any:
        """Call one accessor, returning None (and logging) if it fails."""
        try:
            return fn()
        except Exception as e:
            logger.debug(
                f"Metadata field {label!r} unavailable for {query_handle}: {e}"
            )
            return None

    metadata: dict[str, Any] = {}
    meta = _read(result.metadata, "metadata")
    if meta is None:
        return metadata

    metadata["warnings"] = _read(meta.warnings, "warnings") or []

    metrics = _read(meta.metrics, "metrics")
    if metrics is not None:
        elapsed = _read(metrics.elapsed_time, "elapsed_time")
        execution = _read(metrics.execution_time, "execution_time")
        metadata["metrics"] = {
            # timedelta -> float ms, so the values survive JSON encoding.
            "elapsed_time_ms": elapsed.total_seconds() * 1000
            if elapsed is not None
            else None,
            "execution_time_ms": execution.total_seconds() * 1000
            if execution is not None
            else None,
            "result_count": _read(metrics.result_count, "result_count"),
            "result_size": _read(metrics.result_size, "result_size"),
            "processed_objects": _read(metrics.processed_objects, "processed_objects"),
        }
    return metadata


def run_query_async(
    ctx: Context,
    statement: str,
    copy_to_link: str | None = None,
    copy_to_bucket: str | None = None,
    copy_to_path: str | None = None,
    copy_to_format: str | None = None,
) -> dict[str, Any]:
    """Start a SQL++ query without waiting for it to finish.

    Use for queries expected to take a while. Returns right away with a
    query_handle; it does not return rows. Keep that query_handle: it is
    needed for every follow-up call, and the query holds resources on the
    server until you finish with discard_async_query_results or
    cancel_async_query.

    Pass copy_to_link, copy_to_bucket and copy_to_path to export the results
    to object storage instead of fetching them back. This is the right tool
    for a large export: run_query_sync would hold the request open for the
    entire upload, while this returns as soon as the query is submitted and
    lets you poll for completion. get_async_query_results then reports that
    the export finished and names the destination, rather than carrying rows.

    Usual sequence: get_async_query_results until it reports ready, then
    discard_async_query_results. For quick queries use run_query_sync
    instead, which returns rows directly.

    When the server is in read-only mode, or the caller's token lacks the
    write scope, the query is started with ``QueryOptions(readonly=True)`` —
    same enforcement as run_query_sync, including the client-side
    ``COPY ... TO`` block, since the same read-only guarantee applies to
    async queries.

    Args:
        statement: The SQL++ statement to execute.
        copy_to_link: Name of an existing external link (e.g. an S3 link
            created with CREATE LINK). Required to export.
        copy_to_bucket: Destination bucket in the external store. Required
            to export.
        copy_to_path: Path prefix within that bucket, e.g. "exports/run1".
            Required to export.
        copy_to_format: Output format — json (default) or parquet.

    Returns:
        {"success": True, "query_handle": "..."}, or
        {"success": False, "error": "..."} on failure. When exporting, the
        response also carries "exported": True and "destination": {...}, and
        the eventual results will be empty — the rows go to object storage.
    """
    cluster = get_oi_cluster(ctx)
    registry = get_oi_handle_registry(ctx)

    app_context = ctx.request_context.lifespan_context
    read_only_mode = app_context.read_only_mode

    token = get_access_token()
    lacks_write_scope = token is not None and SCOPE_WRITE not in (token.scopes or [])
    enforce_readonly = read_only_mode or lacks_write_scope

    try:
        statement_to_run, destination = _resolve_copy_to(
            statement,
            link=copy_to_link,
            bucket=copy_to_bucket,
            path=copy_to_path,
            output_format=copy_to_format,
        )
    except CopyToError as e:
        logger.debug(f"Rejecting export request: {e}")
        return tool_error(e, statement=statement)

    # Checks the statement actually being sent, so an export built from the
    # copy_to_* arguments is gated exactly like one the caller wrote by hand.
    if enforce_readonly and _is_copy_to_statement(statement_to_run):
        logger.debug("Blocking COPY ... TO statement under read-only mode")
        return tool_error(
            "COPY ... TO is blocked under read-only mode: the server itself "
            "classifies it as read-only, but it writes its result to "
            "external storage or a KV collection.",
            statement=statement,
        )

    try:
        logger.debug(
            f"Starting async query (readonly={enforce_readonly}, "
            f"export={destination is not None})"
        )
        handle = (
            cluster.start_query(statement_to_run, QueryOptions(readonly=True))
            if enforce_readonly
            else cluster.start_query(statement_to_run)
        )
        # Register the statement actually sent, so the handle registry and any
        # later diagnostics show the COPY that is really running. The
        # destination rides along so get_async_query_results can say where the
        # rows went — by then it has only the token to work from.
        query_handle = registry.register(handle, statement_to_run, destination)
        logger.info(
            f"Started async query (token={query_handle}, "
            f"export={destination is not None})"
        )
        if destination is not None:
            return tool_success(
                query_handle=query_handle,
                exported=True,
                destination=destination,
                message=(
                    f"Export submitted. Call get_async_query_results with this "
                    f"query_handle to check whether it has finished; it will "
                    f"return no rows, as the results are written to "
                    f"{destination['bucket']}/{destination['path']}."
                ),
            )
        return tool_success(
            query_handle=query_handle,
            message=(
                "Query submitted. Call get_async_query_results with this "
                "query_handle to check whether it has finished and retrieve "
                "the rows."
            ),
        )
    except Exception as e:
        logger.error(f"Error starting async query: {e}", exc_info=True)
        return tool_error(e, statement=statement)


def get_async_query_results(ctx: Context, query_handle: str) -> dict[str, Any]:
    """Check the status of an async query and get its results once it has finished.

    This both reports progress and returns results. If the query is still
    running it returns ready: false and no rows, rather than waiting; call it
    again later to check. If it has finished it returns ready: true with the
    rows.

    Each call is a request to the server, so space out repeat calls instead of
    looping tightly — and prefer telling the user the query is still running
    over waiting indefinitely for it.

    Safe to call more than once after it is ready: it does not consume the
    results. When you no longer need them, call discard_async_query_results
    to free them on the server.

    Args:
        query_handle: The query_handle returned by run_query_async.

    Returns:
        {"success": True, "ready": true, "rows": [...], "row_count": N,
        "truncated": bool, "metadata": {"warnings": [...], "metrics":
        {"elapsed_time_ms", "execution_time_ms", "result_count",
        "result_size", "processed_objects"}}}; or {"success": True,
        "ready": false} if not finished; or {"success": False,
        "error": "..."} on failure. When truncated is true the rows are
        incomplete and metadata may be empty, since the server reports it
        only once every row has been read.
    """
    registry = get_oi_handle_registry(ctx)
    try:
        entry = registry.get(query_handle)
        status = entry.handle.fetch_status()
        if not status.results_ready():
            return tool_success(
                query_handle=query_handle,
                ready=False,
                message=(
                    "Query is still running. Call this tool again later "
                    "to check for results."
                ),
            )

        destination = entry.destination
        result = status.result_handle().fetch_results()
        # fetch_results() hands back the same BlockingQueryResult type the sync
        # path returns, so the identical streaming budget applies here.
        bounded = collect_rows_within_budget(
            result.rows(),
            limit_bytes=max_query_result_size_for(ctx),
            service="operational-insights async",
        )
        # Metadata is only populated once the row stream is exhausted, so a
        # truncated read has none to report; _extract_metadata already returns
        # {} rather than raising when it is unavailable.
        metadata = _extract_metadata(result, query_handle)

        # Deliberately NOT evicted: the server keeps the result buffers after
        # a fetch, so the token must stay valid for a re-fetch or an explicit
        # discard.
        if destination is not None:
            # A finished export returns zero rows, which on its own reads as
            # "the query matched nothing". Name the destination so the result
            # is self-describing rather than relying on the caller to recall
            # what run_query_async was asked to do.
            logger.info(
                f"Export complete for async query (token={query_handle}) -> "
                f"{destination['bucket']}/{destination['path']}"
            )
            return tool_success(
                query_handle=query_handle,
                ready=True,
                exported=True,
                destination=destination,
                row_count=0,
                metadata=metadata,
                message=(
                    f"Export complete. Results were written to "
                    f"{destination['bucket']}/{destination['path']} as "
                    f"{destination['format']}. No rows are returned for an "
                    f"export; read them from object storage."
                ),
            )

        logger.info(
            f"Fetched {bounded.row_count} row(s) for async query "
            f"(token={query_handle}, truncated={bounded.truncated})"
        )
        return tool_success(
            query_handle=query_handle,
            ready=True,
            rows=bounded.rows,
            row_count=bounded.row_count,
            metadata=metadata,
            **bounded.as_envelope_fields(),
        )
    except Exception as e:
        logger.error(f"Error fetching async query results: {e}", exc_info=True)
        return tool_error(e, query_handle=query_handle)


def discard_async_query_results(ctx: Context, query_handle: str) -> dict[str, Any]:
    """Free the results of a finished async query on the server.

    Call this when done with a query's results — whether or not you fetched
    them, since fetching does not free them. This is the normal cleanup step
    after get_async_query_results, and the rows cannot be retrieved
    afterwards.

    If the query is still running there is nothing to discard: this returns
    discarded: false and the query_handle stays usable, so use
    cancel_async_query to stop it instead.

    Args:
        query_handle: The query_handle returned by run_query_async.

    Returns:
        {"success": True, "query_handle": "...", "discarded": true}; or
        {"success": True, "discarded": false, "ready": false} if the query has
        not finished; or {"success": False, "error": "..."} on failure.
    """
    registry = get_oi_handle_registry(ctx)
    try:
        entry = registry.get(query_handle)
        status = entry.handle.fetch_status()
        if not status.results_ready():
            return tool_success(
                query_handle=query_handle,
                discarded=False,
                ready=False,
                message=(
                    "Results are not ready yet; nothing to discard. Cancel "
                    "the query with cancel_async_query to stop it."
                ),
            )

        status.result_handle().discard_results()
        registry.remove(query_handle)
        logger.info(f"Discarded results for async query (token={query_handle})")
        return tool_success(query_handle=query_handle, discarded=True)
    except Exception as e:
        logger.error(f"Error discarding async query results: {e}", exc_info=True)
        return tool_error(e, query_handle=query_handle)


def cancel_async_query(ctx: Context, query_handle: str) -> dict[str, Any]:
    """Stop an async query that is still running.

    Use this to abandon a query you no longer want to wait for. On success the
    query stops and the query_handle is no longer usable.

    A query that has already finished cannot be cancelled: this returns
    cancelled: false and the query_handle stays usable, so call
    discard_async_query_results to free its results.

    Args:
        query_handle: The query_handle returned by run_query_async.

    Returns:
        {"success": True, "query_handle": "...", "cancelled": true}, or
        {"success": False, "error": "..."} on failure.
    """
    registry = get_oi_handle_registry(ctx)
    try:
        entry = registry.get(query_handle)

        # Check first: the server answers a cancel for an already-completed
        # query with a bare 404 (no message), and the SDK treats 404 as
        # success — so a blind cancel would report success, evict the token,
        # and strand the result buffers on the server with no handle left to
        # discard them.
        status = entry.handle.fetch_status()
        if status.results_ready():
            # Deliberately KEEP the entry, so the discard this message
            # recommends is still possible.
            logger.info(
                f"Cancel skipped, query already complete (token={query_handle})"
            )
            return tool_success(
                query_handle=query_handle,
                cancelled=False,
                message=(
                    "Query has already completed, so it cannot be cancelled. "
                    "Call discard_async_query_results to free its results."
                ),
            )

        entry.handle.cancel()
        registry.remove(query_handle)
        logger.info(f"Cancelled async query (token={query_handle})")
        return tool_success(query_handle=query_handle, cancelled=True)
    except Exception as e:
        logger.error(f"Error cancelling async query: {e}", exc_info=True)
        return tool_error(e, query_handle=query_handle)
