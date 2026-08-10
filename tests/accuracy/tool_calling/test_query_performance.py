"""Accuracy tests for the query-performance analysis tools.

Covers (all of them):
  - get_longest_running_queries
  - get_most_frequent_queries
  - get_queries_with_largest_response_sizes
  - get_queries_with_large_result_count
  - get_queries_using_primary_index
  - get_queries_not_using_covering_index
  - get_queries_not_selective

Each tool only takes an optional ``limit`` (default 10). For prompts that
don't specify a number we accept any-of {undefined, 10}. For prompts that
do specify a number we require the literal match.
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


def _default_limit() -> Matcher:
    """Match an unspecified limit: absent, null, or the default of 10."""
    return Matcher.any_of(
        Matcher.undefined(),
        Matcher.null(),
        Matcher.number(lambda v: v == 10),
    )


def _exact_limit(n: int) -> Matcher:
    return Matcher.number(lambda v: v == n)


def _build_cases() -> list[AccuracyCase]:
    cases: list[AccuracyCase] = []

    cases.append(
        AccuracyCase(
            test_id="get_longest_running_queries_default",
            prompt=(
                "Which SQL++ queries have been running the longest on average "
                "across this cluster?"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_longest_running_queries",
                    parameters={"limit": _default_limit()},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_longest_running_queries_top_5",
            prompt="Show me the top 5 longest-running SQL++ queries by average service time.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_longest_running_queries",
                    parameters={"limit": _exact_limit(5)},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_most_frequent_queries_default",
            prompt="Which SQL++ queries are executed most frequently on this cluster?",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_most_frequent_queries",
                    parameters={"limit": _default_limit()},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_most_frequent_queries_top_20",
            prompt="Give me the 20 most frequent SQL++ queries that have run on the cluster.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_most_frequent_queries",
                    parameters={"limit": _exact_limit(20)},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_queries_with_largest_response_sizes",
            prompt=(
                "Which queries return the largest response payloads (largest "
                "average result size in bytes)?"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_queries_with_largest_response_sizes",
                    parameters={"limit": _default_limit()},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_queries_with_large_result_count",
            prompt=(
                "Find the queries that return the most documents — the ones "
                "with the largest result counts."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_queries_with_large_result_count",
                    parameters={"limit": _default_limit()},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_queries_using_primary_index",
            prompt=(
                "Show me which SQL++ queries on this cluster are scanning a "
                "primary index (a sign they probably need a secondary index)."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_queries_using_primary_index",
                    parameters={"limit": _default_limit()},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_queries_not_using_covering_index",
            prompt=(
                "Find queries that scanned an index but still had to do "
                "document fetches — i.e. they are not using a covering index."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_queries_not_using_covering_index",
                    parameters={"limit": _default_limit()},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_queries_not_selective",
            prompt=(
                "List the non-selective queries — ones whose index scan reads "
                "far more documents than the final result count."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_queries_not_selective",
                    parameters={"limit": _default_limit()},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="conversational_slow_queries",
            prompt=(
                "Tell me which SQL++ queries on my cluster are taking forever to "
                "run — I want to know the biggest time hogs."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_longest_running_queries",
                    parameters=Matcher.any_value(),
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="conversational_what_runs_constantly",
            prompt=(
                "Which queries get fired off over and over again? I want to see "
                "the most-executed ones."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_most_frequent_queries",
                    parameters=Matcher.any_value(),
                ),
            ],
        )
    )

    return cases


@pytest.fixture()
def performance_cases():
    return _build_cases()


PERFORMANCE_CASE_IDS = [
    "get_longest_running_queries_default",
    "get_longest_running_queries_top_5",
    "get_most_frequent_queries_default",
    "get_most_frequent_queries_top_20",
    "get_queries_with_largest_response_sizes",
    "get_queries_with_large_result_count",
    "get_queries_using_primary_index",
    "get_queries_not_using_covering_index",
    "get_queries_not_selective",
    "conversational_slow_queries",
    "conversational_what_runs_constantly",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", PERFORMANCE_CASE_IDS)
async def test_performance_tool_accuracy(
    case_id: str,
    performance_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in performance_cases if c.test_id == case_id)
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
