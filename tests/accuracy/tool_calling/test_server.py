"""Accuracy tests for the server / cluster tools.

Covers:
  - get_buckets_in_cluster
  - get_server_configuration_status
  - test_cluster_connection (with and without bucket)
  - get_scopes_in_bucket
  - get_collections_in_scope
  - get_scopes_and_collections_in_bucket
  - get_cluster_health_and_services (including service_types filtering)
  - get_cluster_diagnostics_report
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


def _optional_bucket() -> Matcher:
    """Match an absent or null ``bucket_name`` argument.

    The LLM may either omit the optional bucket_name entirely or pass it as
    ``null`` — both should score 1.0.
    """
    return Matcher.any_of(Matcher.undefined(), Matcher.null())


def _build_cases(bucket: str, scope: str) -> list[AccuracyCase]:
    cases: list[AccuracyCase] = []

    cases.append(
        AccuracyCase(
            test_id="get_buckets_in_cluster",
            prompt="List all buckets that are available in my Couchbase cluster.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_buckets_in_cluster",
                    parameters={},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_server_configuration_status",
            prompt=(
                "Show me the current configuration status of the Couchbase MCP "
                "server (the server's own settings, read-only mode, etc.) — "
                "do not connect to any bucket."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_server_configuration_status",
                    parameters={},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="test_cluster_connection_no_bucket",
            prompt=(
                "Verify that the connection to the Couchbase cluster works. "
                "Do not target any specific bucket."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="test_cluster_connection",
                    parameters={"bucket_name": _optional_bucket()},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="test_cluster_connection_with_bucket",
            prompt=(
                f"Test whether the connection to bucket '{bucket}' is working "
                "from the Couchbase cluster."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="test_cluster_connection",
                    parameters={"bucket_name": bucket},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_scopes_in_bucket",
            prompt=f"List only the scopes (not the collections) in bucket '{bucket}'.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_scopes_in_bucket",
                    parameters={"bucket_name": bucket},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_collections_in_scope",
            prompt=(
                f"List the collections inside scope '{scope}' of bucket '{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_collections_in_scope",
                    parameters={"bucket_name": bucket, "scope_name": scope},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_scopes_and_collections_in_bucket",
            prompt=(
                f"Give me a complete map of every scope and the collections each "
                f"scope contains for bucket '{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_scopes_and_collections_in_bucket",
                    parameters={"bucket_name": bucket},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_cluster_health_and_services_no_bucket",
            prompt=(
                "What is the health of my Couchbase cluster, and which services "
                "are currently running? Use a cluster-level ping."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_cluster_health_and_services",
                    parameters={"bucket_name": _optional_bucket()},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_cluster_health_and_services_with_bucket",
            prompt=(
                f"Ping the Couchbase services from the perspective of bucket "
                f"'{bucket}' and report their health."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_cluster_health_and_services",
                    parameters={"bucket_name": bucket},
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_cluster_health_and_services_query_service_only",
            prompt=(
                "Check right now whether just the query service on my Couchbase "
                "cluster is reachable — don't check any other service."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_cluster_health_and_services",
                    parameters={
                        "bucket_name": _optional_bucket(),
                        "service_types": ["query"],
                    },
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="conversational_what_buckets_do_i_have",
            prompt="What buckets do I actually have on this Couchbase cluster?",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_buckets_in_cluster",
                    parameters=Matcher.any_value(),
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="conversational_is_everything_healthy",
            prompt=(
                "Is my Couchbase cluster healthy? I want to know whether all the "
                "services are up and reachable."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_cluster_health_and_services",
                    parameters=Matcher.any_value(),
                ),
            ],
        )
    )

    return cases


@pytest.fixture()
def server_cases(test_bucket: str, test_scope: str):
    return _build_cases(test_bucket, test_scope)


SERVER_CASE_IDS = [
    "get_buckets_in_cluster",
    "get_server_configuration_status",
    "test_cluster_connection_no_bucket",
    "test_cluster_connection_with_bucket",
    "get_scopes_in_bucket",
    "get_collections_in_scope",
    "get_scopes_and_collections_in_bucket",
    "get_cluster_health_and_services_no_bucket",
    "get_cluster_health_and_services_with_bucket",
    "get_cluster_health_and_services_query_service_only",
    "conversational_what_buckets_do_i_have",
    "conversational_is_everything_healthy",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", SERVER_CASE_IDS)
async def test_server_tool_accuracy(
    case_id: str,
    server_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in server_cases if c.test_id == case_id)
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
