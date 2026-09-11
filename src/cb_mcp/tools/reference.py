"""Tool for looking up the exact input values another tool requires.

Backed by reference datasets bundled under ``cb_mcp/reference_data`` -- see the README there for
the format. This tool never touches the cluster, so it keeps working when the cluster is
unreachable, which is exactly when someone is likely to be looking up a metric name.

Error handling: everything is reported through ``tool_success``/``tool_error``
(``utils/responses.py``) rather than raised. There is no cluster call here, so the "let connection
failures propagate" carve-out other read tools use does not apply. Each error carries the valid
values for whatever was wrong, so the caller can correct itself in one round trip instead of
guessing again.
"""

import json
import logging
from typing import Any

from ..utils.constants import MCP_SERVER_NAME
from ..utils.reference_data import (
    MAX_LIST_RESPONSE_BYTES,
    chapters,
    dataset_size_bytes,
    list_records,
    load_envelope,
    registered_tool_names,
    resolve_dataset,
    search,
)
from ..utils.responses import tool_error, tool_success

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tools.reference")


def discover_tool_input_values(
    tool_name: str,
    search_keywords: list[str] | None = None,
    chapter_filters: dict[str, str] | None = None,
    max_results: int = 25,
    min_score: float = 0.0,
) -> dict[str, Any]:
    """Find the exact input values a specific tool requires, from reference data bundled with this server.

    Call this BEFORE calling a tool that needs an exact identifier you do not already know. Do not
    guess the identifier and do not browse the Couchbase documentation website for it -- a guessed
    value comes back from the target tool as an error with no data. The reference data ships inside
    this server, so this works offline and while the cluster is unreachable, which is exactly when
    you are most likely to be looking up a metric name.

    Registered today:
    - tool_name="get_cluster_metrics" -> every Couchbase Server metric name, with its type
      (counter/gauge/histogram), unit, the server version it first appeared in, and a one-line
      description. Feed a result's "name" straight into get_cluster_metrics.

    tool_name is the only required argument; everything else is optional.

    Two ways to call it:
    1. List -- pass tool_name alone. Returns EVERY record in the dataset, plus the "chapters" block
       (the small set of fields you can filter on, with every legal value and a record count each).
       This is a large response: prefer search when you already know roughly what you want, and use
       the full list when you need to see the whole namespace. A dataset too large to list returns
       chapters and a "next_step" telling you to search instead of a partial list.
    2. Search -- pass tool_name and search_keywords. Returns records ranked by fuzzy relevance, best
       first. Matching is fuzzy over identifiers and descriptions, so partial words and near-misses
       still hit.

    Every response includes the chapters block, so you never need a separate browse call before
    narrowing.

    Args:
        tool_name: REQUIRED. The tool you are about to call, e.g. "get_cluster_metrics". An
          unrecognised value returns the list of registered names, so you do not need to guess twice.
        search_keywords: A LIST of words describing what you are looking for, e.g.
          ["disk", "write", "queue"] or ["index", "resident", "ratio"]. Keep each concept a separate
          list item. Omit to browse instead of searching.
        chapter_filters: Optional narrowing, as {chapter_field: value}, e.g.
          {"category": "Query Service Metrics"}. Only fields listed in the response's "chapters" are
          accepted (a dataset without chapters accepts none), and multiple filters are combined with
          AND. Strongly recommended when you know the service: identifiers use short prefixes (kv_,
          n1ql_, fts_) that keyword search cannot connect to service names, so an unfiltered search
          for "query service memory" can return Index Service metrics instead.
        max_results: How many records to return. NOT capped by this server -- you are responsible for
          choosing a value whose response fits your client's output-size limit. If results are too
          broad, add keywords or a chapter filter rather than raising this.
        min_score: Relevance floor, 0-100. Defaults to 0, meaning nothing is filtered out and you
          always get the best max_results matches ranked by score, however weak. Check each
          result's "score" to judge quality -- low scores across the board mean your keywords
          missed, so change them rather than reading further down the list. Raise this only to
          suppress weak matches you have already seen.

    Returns {"success": True, "dataset": ..., "chapters": ..., ...} plus either "record_count" and
    "records" (the full list) or "matches" (total found), "returned" and "results" (ranked, each
    record plus its "score") (search). On failure returns {"success": False, "error": ...} listing the
    valid values for whatever was wrong -- e.g. an unrecognised tool_name comes back with
    "available_tool_names", a bad chapter filter with "valid_chapter_fields" or "valid_values".
    Zero matches is a successful response with an empty "results" list -- refine your keywords, drop
    a chapter_filter, or lower min_score and call again.
    """
    dataset_path = resolve_dataset(tool_name)
    if dataset_path is None:
        message = f"No reference data is registered for tool_name={tool_name!r}."
        logger.warning(f"Rejected discover_tool_input_values request: {message}")
        return tool_error(message, available_tool_names=registered_tool_names())

    try:
        envelope = load_envelope(dataset_path)
        dataset_chapters = chapters(dataset_path)
    except Exception as e:
        logger.error(
            f"Failed to read reference dataset {dataset_path}: {e}", exc_info=True
        )
        return tool_error(e, tool_name=tool_name)

    # Every bad filter is reported at once, not just the first: returning on the first one would
    # make the caller fix it, retry, and only then discover the second. The response carries the
    # full chapter listing, so one round trip is enough to correct all of them.
    filters = chapter_filters or {}
    invalid_filters: list[dict[str, Any]] = []
    for field, value in filters.items():
        if field not in dataset_chapters:
            invalid_filters.append(
                {
                    "filter": field,
                    "problem": (
                        f"{field!r} is not a chapter field for this dataset, so it cannot "
                        "be filtered on."
                    ),
                }
            )
        elif value not in dataset_chapters[field]:
            invalid_filters.append(
                {
                    "filter": field,
                    "problem": f"{value!r} is not a value of chapter {field!r}.",
                    "valid_values": sorted(dataset_chapters[field]),
                }
            )

    if invalid_filters:
        message = "Invalid chapter_filters: " + " ".join(
            entry["problem"] for entry in invalid_filters
        )
        logger.warning(f"Rejected discover_tool_input_values request: {message}")
        return tool_error(
            message,
            invalid_filters=invalid_filters,
            valid_chapter_fields=sorted(dataset_chapters),
            chapters=dataset_chapters,
        )

    # A bare string instead of a list is a harmless caller slip -- wrap it rather than spending a
    # round trip on an error.
    if isinstance(search_keywords, str):
        search_keywords = [search_keywords]

    query = " ".join(
        str(keyword) for keyword in (search_keywords or []) if str(keyword).strip()
    )
    dataset_summary = {
        "dataset_id": envelope.get("dataset_id"),
        "title": envelope.get("title"),
        "record_count": envelope.get("record_count"),
        "source_url": envelope.get("source_url"),
        "generated_at": envelope.get("generated_at"),
    }

    if not query:
        # No keywords: hand back the whole dataset so the caller can pick an identifier without a
        # second call -- but only if the response actually fits. An over-size listing is not
        # truncated, because a partial slice reads as the complete namespace; the caller is told to
        # search instead.
        #
        # Two checks, cheapest first. The file size is a lower bound on the response, so a dataset
        # far too big is rejected without ever being read. Anything that clears that is built and
        # measured for real, because the response carries the dataset/chapters blocks on top of the
        # records and can cross the limit even when the file did not.
        file_bytes = dataset_size_bytes(dataset_path)
        declared_count = envelope.get("record_count")
        listing: dict[str, Any] = {}
        response_bytes = None

        if file_bytes <= MAX_LIST_RESPONSE_BYTES:
            records = list_records(dataset_path)
            candidate = tool_success(
                dataset=dataset_summary,
                chapters=dataset_chapters,
                record_count=len(records),
                records=records,
                next_step=(
                    "Every record is listed above. To narrow instead of scanning, call again "
                    "with search_keywords, optionally with chapter_filters."
                ),
            )
            response_bytes = len(json.dumps(candidate, default=str))
            if response_bytes <= MAX_LIST_RESPONSE_BYTES:
                logger.info(
                    f"discover_tool_input_values({tool_name!r}) listed {len(records)} record(s) "
                    f"({response_bytes} bytes)"
                )
                return candidate
            declared_count = len(records)

        # Too large to list. Log the measured size so a client-side rejection is explainable.
        logger.info(
            f"discover_tool_input_values({tool_name!r}) not listing in full: "
            f"file {file_bytes} bytes, response "
            f"{response_bytes if response_bytes is not None else 'not built'} bytes, "
            f"limit {MAX_LIST_RESPONSE_BYTES} bytes"
        )
        listing = {
            "record_count": declared_count,
            "next_step": (
                f"This dataset has {declared_count} records and is too large to list in one "
                f"response (limit {MAX_LIST_RESPONSE_BYTES // 1024} KB). Call again with "
                "search_keywords describing what you need, optionally narrowed with "
                "chapter_filters, to get the matching records."
            ),
        }
        return tool_success(
            dataset=dataset_summary, chapters=dataset_chapters, **listing
        )

    try:
        results, total_matches = search(
            dataset_path,
            query,
            chapter_filters=filters,
            max_results=max_results,
            min_score=min_score,
        )
    except Exception as e:
        logger.error(
            f"Reference data search failed for {tool_name!r}: {e}", exc_info=True
        )
        return tool_error(e, tool_name=tool_name)

    logger.info(
        f"discover_tool_input_values({tool_name!r}) matched {total_matches} record(s), "
        f"returning {len(results)}"
    )
    response = tool_success(
        dataset=dataset_summary,
        chapters=dataset_chapters,
        filters_applied=filters,
        matches=total_matches,
        returned=len(results),
        results=results,
    )
    if total_matches == 0:
        response["next_step"] = (
            "Nothing matched. Try different or fewer search_keywords, drop a chapter_filter, "
            "or lower min_score."
        )
    return response
