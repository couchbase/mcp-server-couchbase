"""Accuracy tests for the FTS tools.

Covers:
  - list_fts_indexes (no filter, bucket+scope filter)
  - get_fts_index_definition
  - run_fts_query (normal query, explain=True)
  - Disambiguation: a full-text relevance-search prompt should select
    run_fts_query rather than run_sql_plus_plus_query's SEARCH() function.
"""

from __future__ import annotations

import json
import uuid

import pytest

from accuracy.sdk import (
    AccuracyCase,
    DiskResultStorage,
    Matcher,
    OpenAIAgent,
    drop_fts_index,
    run_accuracy_case,
    seed_fts_index,
)
from accuracy.sdk.types import ExpectedToolCall


def _optional() -> Matcher:
    return Matcher.any_of(Matcher.undefined(), Matcher.null())


def _build_cases(bucket: str, scope: str, collection: str) -> list[AccuracyCase]:
    cases: list[AccuracyCase] = []
    index_name = f"acc_fts_idx_{uuid.uuid4().hex[:8]}"
    seed = seed_fts_index(bucket, scope, collection, index_name)
    cleanup = drop_fts_index(bucket, scope, index_name)

    cases.append(
        AccuracyCase(
            test_id="list_fts_indexes_no_filter",
            prompt="List every Search (full-text search) index in the cluster.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="list_fts_indexes",
                    parameters={
                        "bucket_name": _optional(),
                        "scope_name": _optional(),
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        )
    )

    cases.append(
        AccuracyCase(
            test_id="list_fts_indexes_scoped",
            prompt=(
                f"List the Search indexes defined in scope '{scope}' of bucket "
                f"'{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="list_fts_indexes",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_fts_index_definition",
            prompt=(
                f"Show me the full definition of the Search index named "
                f"'{index_name}' in scope '{scope}' of bucket '{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_fts_index_definition",
                    parameters={
                        "index_name": index_name,
                        "bucket_name": bucket,
                        "scope_name": scope,
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        )
    )

    cases.append(
        AccuracyCase(
            test_id="run_fts_query",
            prompt=(
                f"Using the Search index '{index_name}' in scope '{scope}' of "
                f"bucket '{bucket}', run a search query that matches all "
                f"documents (a match_all query)."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_fts_query",
                    parameters={
                        "index_name": index_name,
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "query": Matcher.any_value(),
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        )
    )

    cases.append(
        AccuracyCase(
            test_id="run_fts_query_explain",
            prompt=(
                f"Explain the execution plan for a match_all query against the "
                f"Search index '{index_name}' in scope '{scope}' of bucket "
                f"'{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_fts_query",
                    parameters={
                        "index_name": index_name,
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "query": Matcher.any_value(),
                        "explain": True,
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        )
    )

    cases.append(
        AccuracyCase(
            test_id="run_fts_query_over_sql_plus_plus",
            prompt=(
                f"Search the '{index_name}' Search index in scope '{scope}' of "
                f"bucket '{bucket}' for documents whose text fuzzily matches "
                f"'ale', ranked by relevance score."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_fts_query",
                    parameters={
                        "index_name": index_name,
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "query": Matcher.any_value(),
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        )
    )

    return cases


@pytest.fixture()
def fts_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


FTS_CASE_IDS = [
    "list_fts_indexes_no_filter",
    "list_fts_indexes_scoped",
    "get_fts_index_definition",
    "run_fts_query",
    "run_fts_query_explain",
    "run_fts_query_over_sql_plus_plus",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", FTS_CASE_IDS)
async def test_fts_tool_accuracy(
    case_id: str,
    fts_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in fts_cases if c.test_id == case_id)
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
