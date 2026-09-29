"""Accuracy tests for the vector search tools.

Covers:
  - run_vector_search: no index_name (GSI selects the index automatically),
    unlike every other search/query tool that takes one.
  - run_search_vector_search: index_name required, plus the hybrid
    (scalar_query present) vs. vector-only distinction.
  - Disambiguation: a plain full-text relevance prompt should still select
    run_fts_query, not one of these two, and a "find similar/semantically
    related" prompt should select run_vector_search over run_fts_query.

No seed/cleanup hooks: these cases score tool selection and parameters (see
accuracy/sdk/scorer.py), not whether the underlying index exists or the call
actually returns hits, so no live vector index is required — matching the
"conversational" style cases in the other accuracy test files. index_name in
the run_search_vector_search cases is a plausible, not necessarily-existing,
name for the same reason.
"""

from __future__ import annotations

import json

import pytest

from accuracy.sdk import (
    AccuracyCase,
    DiskResultStorage,
    Matcher,
    OpenAIAgent,
    run_accuracy_case,
)
from accuracy.sdk.types import ExpectedToolCall


def _build_cases(bucket: str, scope: str, collection: str) -> list[AccuracyCase]:
    index_name = "product_vector_index"

    return [
        AccuracyCase(
            test_id="run_vector_search_no_index_name",
            prompt=(
                f"In the '{collection}' collection (scope '{scope}', bucket "
                f"'{bucket}'), find documents whose 'embedding' field is "
                f"semantically closest to the text 'a warm winter jacket'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_vector_search",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "vector_field": "embedding",
                        "query_text": Matcher.any_value(),
                    },
                ),
            ],
        ),
        AccuracyCase(
            test_id="run_search_vector_search_named_index",
            prompt=(
                f"Using the Search index '{index_name}', run a vector "
                f"similarity search on the 'embedding' field for text "
                f"semantically similar to 'a warm winter jacket'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_search_vector_search",
                    parameters={
                        "index_name": index_name,
                        "vector_field": "embedding",
                        "vector_query_text": Matcher.any_value(),
                    },
                ),
            ],
        ),
        AccuracyCase(
            test_id="run_search_vector_search_hybrid",
            prompt=(
                f"Using the Search index '{index_name}', find products whose "
                f"'embedding' field is semantically similar to 'a warm winter "
                f"jacket', but only among products whose description also "
                f"contains the word 'jacket'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_search_vector_search",
                    parameters={
                        "index_name": index_name,
                        "vector_field": "embedding",
                        "vector_query_text": Matcher.any_value(),
                        "scalar_query": Matcher.any_value(),
                    },
                ),
            ],
        ),
        AccuracyCase(
            test_id="conversational_plain_fts_not_vector_search",
            prompt=(
                f"In the '{collection}' collection (scope '{scope}', bucket "
                f"'{bucket}'), search the '{index_name}' Search index for "
                f"documents whose text fuzzily matches the word 'jacket', "
                f"ranked by relevance score."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_fts_query",
                    parameters=Matcher.any_value(),
                ),
            ],
        ),
    ]


@pytest.fixture()
def vector_search_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


VECTOR_SEARCH_CASE_IDS = [
    "run_vector_search_no_index_name",
    "run_search_vector_search_named_index",
    "run_search_vector_search_hybrid",
    "conversational_plain_fts_not_vector_search",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", VECTOR_SEARCH_CASE_IDS)
async def test_vector_search_tool_accuracy(
    case_id: str,
    vector_search_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in vector_search_cases if c.test_id == case_id)
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
