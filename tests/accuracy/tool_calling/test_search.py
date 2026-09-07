"""Accuracy tests for the FTS/Search tools.

Covers:
  - list_search_indexes (no filter, bucket+scope filter)
  - get_search_index_definition
  - run_fts_query
  - explain_fts_query
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
    drop_search_index,
    run_accuracy_case,
    seed_search_index,
)
from accuracy.sdk.types import ExpectedToolCall


def _optional() -> Matcher:
    return Matcher.any_of(Matcher.undefined(), Matcher.null())


def _build_cases(bucket: str, scope: str, collection: str) -> list[AccuracyCase]:
    cases: list[AccuracyCase] = []
    index_name = f"acc_fts_idx_{uuid.uuid4().hex[:8]}"
    seed = seed_search_index(bucket, scope, collection, index_name)
    cleanup = drop_search_index(bucket, scope, index_name)

    cases.append(
        AccuracyCase(
            test_id="list_search_indexes_no_filter",
            prompt="List every Search (full-text search) index in the cluster.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="list_search_indexes",
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
            test_id="list_search_indexes_scoped",
            prompt=(
                f"List the Search indexes defined in scope '{scope}' of bucket "
                f"'{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="list_search_indexes",
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
            test_id="get_search_index_definition",
            prompt=(
                f"Show me the full definition of the Search index named "
                f"'{index_name}' in scope '{scope}' of bucket '{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_search_index_definition",
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
            test_id="explain_fts_query",
            prompt=(
                f"Explain the execution plan for a match_all query against the "
                f"Search index '{index_name}' in scope '{scope}' of bucket "
                f"'{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="explain_fts_query",
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
def search_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


SEARCH_CASE_IDS = [
    "list_search_indexes_no_filter",
    "list_search_indexes_scoped",
    "get_search_index_definition",
    "run_fts_query",
    "explain_fts_query",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", SEARCH_CASE_IDS)
async def test_search_tool_accuracy(
    case_id: str,
    search_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in search_cases if c.test_id == case_id)
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
