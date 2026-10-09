"""Accuracy tests for the Operational Insights query tools.

Covers:
  - oi_run_query_sync (plain SELECT, count, and the copy_to_* export form)
  - oi_explain_query
  - oi_run_query_async

The async *lifecycle* tools (get results / discard / cancel) take a
``query_handle`` that only exists after a real async query has been started,
so they are scored in test_oi_async_lifecycle.py rather than here.

As in the operational query tests, the ``statement`` parameter is matched
loosely with a substring predicate: the model may format SQL++ whitespace
differently, alias columns, or quote identifiers. Tool selection is the
primary signal; the statement body is checked semantically only where it
carries the meaning of the case.

The sync-vs-async choice is the interesting accuracy question in this file.
Both tools accept the same statement, and the tool descriptions say async is
for long-running queries and large exports — so there are paired cases below
that differ only in how the prompt frames the cost of the query.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from accuracy.sdk import (
    AccuracyCase,
    DiskResultStorage,
    Matcher,
    OpenAIAgent,
    run_accuracy_case,
)
from accuracy.sdk.types import ExpectedToolCall

from .._seeding import drop_scope, seed_collection, unique_name

_DOCS = [
    {"id": "oiacc-1", "name": "alpha", "rating": 5},
    {"id": "oiacc-2", "name": "beta", "rating": 3},
    {"id": "oiacc-3", "name": "gamma", "rating": 4},
]


def _contains(*needles: str) -> Matcher:
    """Match a string containing every (case-insensitive) needle."""
    lowered = [n.lower() for n in needles]
    return Matcher.string(lambda value: all(n in value.lower() for n in lowered))


def _plan_statement(collection: str) -> Matcher:
    """Match the statement handed to ``oi_explain_query``.

    Two conditions in one predicate (the Matcher hierarchy has ``any_of`` but
    no ``all_of``, so they are combined here rather than composed): it must
    look like the SELECT we asked about, and it must NOT already start with
    EXPLAIN — the tool documents that it adds the keyword itself, so a model
    that prepends it produces ``EXPLAIN EXPLAIN ...`` on the server.
    """

    def _ok(value: str) -> bool:
        lowered = value.lower()
        return (
            "select" in lowered
            and collection.lower() in lowered
            and not lowered.strip().startswith("explain")
        )

    return Matcher.string(_ok)


def _export_parameters(collection: str) -> dict[str, Any]:
    """The copy_to_* arguments an export must carry, whichever tool runs it."""
    return {
        "statement": _contains("select", collection),
        "copy_to_link": "s3link",
        "copy_to_bucket": "analytics-exports",
        "copy_to_path": "exports/run1",
        "copy_to_format": Matcher.case_insensitive_string("parquet"),
    }


def _mock_export(args: dict[str, Any]) -> str:
    """Stand in for a real COPY ... TO against object storage."""
    return json.dumps(
        {
            "success": True,
            "exported": True,
            "destination": {
                "link": args.get("copy_to_link"),
                "bucket": args.get("copy_to_bucket"),
                "path": args.get("copy_to_path"),
                "format": args.get("copy_to_format", "json"),
            },
        }
    )


def _build_cases(database: str, scope: str, collection: str) -> list[AccuracyCase]:
    ks = f"`{database}`.`{scope}`.`{collection}`"
    seed = seed_collection(database, scope, collection, documents=_DOCS)
    cleanup = drop_scope(database, scope)

    return [
        AccuracyCase(
            test_id="oi_run_query_sync_select",
            prompt=(
                f"Run this SQL++ statement and show me the rows: "
                f"SELECT * FROM {ks} LIMIT 5"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_run_query_sync",
                    parameters={
                        "statement": _contains("select", collection),
                        # The export arguments must stay unset for a plain read:
                        # passing them would silently redirect rows to object
                        # storage and return none.
                        "copy_to_link": Matcher.any_of(
                            Matcher.undefined(), Matcher.null()
                        ),
                        "copy_to_bucket": Matcher.any_of(
                            Matcher.undefined(), Matcher.null()
                        ),
                        "copy_to_path": Matcher.any_of(
                            Matcher.undefined(), Matcher.null()
                        ),
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        ),
        AccuracyCase(
            test_id="oi_run_query_sync_count",
            prompt=(
                f"How many documents are in the '{collection}' collection in "
                f"scope '{scope}' of database '{database}'? Run a query that "
                "returns the count."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_run_query_sync",
                    parameters={"statement": _contains("count", collection)},
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        ),
        AccuracyCase(
            test_id="oi_explain_query",
            prompt=(
                f"Show me the query execution plan for SELECT * FROM {ks} "
                "WHERE rating > 3 — just the plan, do not actually run it."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_explain_query",
                    parameters={
                        "statement": _plan_statement(collection),
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        ),
        # Paired with oi_run_query_sync_select: same shape of query, but the
        # prompt makes the cost explicit, which is what should tip the model
        # from the sync tool to the async one.
        AccuracyCase(
            test_id="oi_run_query_async_long_running",
            prompt=(
                f"I need to run a heavy aggregation over {ks} that will take a "
                "long time. Start it in the background and give me a handle I "
                "can poll — do not block waiting for it. The statement is: "
                f"SELECT name, COUNT(*) AS n FROM {ks} GROUP BY name"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_run_query_async",
                    parameters={"statement": _contains("select", collection)},
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        ),
        # The export form. Scored on the copy_to_* arguments rather than on
        # which of the two query tools carries them: both accept the same
        # export parameters, and their docstrings actively point at each
        # other ("For a large export prefer oi_run_query_async" /
        # "This is the right tool for a large export"), so either choice is
        # correct here. Pinning this to oi_run_query_sync would be testing a
        # coin flip the docs deliberately leave open.
        #
        # Both tools are mocked: a real COPY ... TO needs an external S3 link
        # that the accuracy cluster is not guaranteed to have, and this case
        # scores argument selection, not the upload. Mocking only one would
        # let the other reach the cluster and fail on the missing link.
        AccuracyCase(
            test_id="oi_run_query_export_copy_to",
            prompt=(
                f"Export the full contents of {ks} to object storage instead "
                "of returning the rows to me. Use the existing external link "
                "named 's3link', bucket 'analytics-exports', path "
                "'exports/run1', in parquet format."
            ),
            # Scored by test_oi_export_accuracy below rather than by the
            # shared scorer: marking both tools optional would make the case
            # pass even if the model called neither.
            expected_tools=[],
            seed=seed,
            cleanup=cleanup,
            mocks={
                "oi_run_query_sync": _mock_export,
                "oi_run_query_async": _mock_export,
            },
        ),
    ]


@pytest.fixture()
def query_cases(oi_database: str, oi_scope: str):
    # Query cases seed their own scope + collection, so they do not depend on
    # CB_OI_TEST_COLLECTION pointing at pre-existing data.
    scope = unique_name("oiacc_scope")
    collection = unique_name("oiacc_coll")
    return _build_cases(oi_database, scope, collection)


QUERY_CASE_IDS = [
    "oi_run_query_sync_select",
    "oi_run_query_sync_count",
    "oi_explain_query",
    "oi_run_query_async_long_running",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", QUERY_CASE_IDS)
async def test_oi_query_tool_accuracy(
    case_id: str,
    query_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in query_cases if c.test_id == case_id)
    result = await run_accuracy_case(
        case,
        accuracy_client_factory=accuracy_client,
        openai_agent=openai_agent,
        openai_model=openai_model,
        result_storage=result_storage,
        accuracy_run_id=accuracy_run_id,
        commit_sha=commit_sha,
    )

    assert result.accuracy >= 0.75, (
        f"Accuracy for case '{case_id}' was {result.accuracy}. "
        f"Expected: {case.expected_tools}. "
        f"Actual: {json.dumps([c.__dict__ for c in result.actual_calls], indent=2, default=str)}"
    )


@pytest.mark.asyncio
async def test_oi_export_accuracy(
    query_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    """The export case, scored against *either* query tool.

    ``calculate_tool_calling_accuracy`` cannot express "one of these two
    tools, with these arguments": an expectation where every entry is
    optional is satisfied by calling nothing at all (verified — it returns
    1.0 for an empty call list). So the call list is asserted directly here.

    What is actually being tested is the ``copy_to_*`` arguments. Whether the
    export rides on the sync or the async tool is left open on purpose, since
    both accept the same parameters and each tool's docstring recommends the
    other for a large export.
    """
    case = next(c for c in query_cases if c.test_id == "oi_run_query_export_copy_to")
    result = await run_accuracy_case(
        case,
        accuracy_client_factory=accuracy_client,
        openai_agent=openai_agent,
        openai_model=openai_model,
        result_storage=result_storage,
        accuracy_run_id=accuracy_run_id,
        commit_sha=commit_sha,
    )

    actual = json.dumps(
        [c.__dict__ for c in result.actual_calls], indent=2, default=str
    )
    export_calls = [
        call
        for call in result.actual_calls
        if call.tool_name in ("oi_run_query_sync", "oi_run_query_async")
    ]
    assert export_calls, f"No query tool was called at all. Actual: {actual}"

    expected = _export_parameters("")
    matched = [
        call
        for call in export_calls
        if all(
            Matcher.value(expected[key]).match(call.parameters.get(key)) >= 0.75
            for key in (
                "copy_to_link",
                "copy_to_bucket",
                "copy_to_path",
                "copy_to_format",
            )
        )
    ]
    assert matched, (
        "A query tool ran, but none carried the requested copy_to_* export "
        f"arguments. Actual: {actual}"
    )
