"""Result-validation evals for the server / cluster tools (LLM-as-judge).

Cluster topology and health are live state, not seeded ground truth, so every
case is a faithfulness check: the answer must be consistent with the tool
output and must not invent buckets / scopes / collections / services.

The one anchored fact we can rely on is that the configured test bucket
exists, so the buckets case additionally requires it to be listed.
"""

from __future__ import annotations

import pytest

from accuracy.sdk import ResultCase

from ._harness import assert_result_case


def _build_cases(bucket: str, scope: str, collection: str) -> list[ResultCase]:
    cases: list[ResultCase] = []

    cases.append(
        ResultCase(
            test_id="buckets_lists_test_bucket",
            prompt="What buckets are available on this Couchbase cluster?",
            expectation=(
                f"The tool returns the cluster's bucket list, which includes "
                f"'{bucket}'. A correct answer lists the buckets returned by the "
                f"tool and includes '{bucket}'. FAIL if it omits '{bucket}' or "
                "invents buckets not present in the tool output."
            ),
        )
    )

    cases.append(
        ResultCase(
            test_id="scopes_faithful",
            prompt=f"List the scopes in bucket '{bucket}'.",
            expectation=(
                f"Faithfulness check. The answer must reflect the scopes the "
                f"tool returned for bucket '{bucket}' (it should include scope "
                f"'{scope}'). FAIL if it invents scopes not in the tool output "
                f"or omits the scope '{scope}' that the tool returned."
            ),
        )
    )

    cases.append(
        ResultCase(
            test_id="collections_faithful",
            prompt=(
                f"What collections are inside scope '{scope}' of bucket '{bucket}'?"
            ),
            expectation=(
                "Faithfulness check. The answer must reflect the collections the "
                "tool returned for that scope and must not invent collections "
                "absent from the tool output."
            ),
        )
    )

    cases.append(
        ResultCase(
            test_id="scopes_and_collections_faithful",
            prompt=(
                f"Give me the full map of scopes and their collections for "
                f"bucket '{bucket}'."
            ),
            expectation=(
                "Faithfulness check. The answer must reflect the scope-to-"
                "collection mapping the tool returned and must not invent scopes "
                "or collections that are not in the tool output."
            ),
        )
    )

    cases.append(
        ResultCase(
            test_id="health_faithful",
            prompt="Is my Couchbase cluster healthy? Which services are running?",
            expectation=(
                "Faithfulness check. The answer must reflect the health/ping "
                "result the tool returned — the services and their status as "
                "reported. FAIL only if it asserts a health status or services "
                "that contradict the tool output, or fabricates services not "
                "present in it."
            ),
        )
    )

    cases.append(
        ResultCase(
            test_id="config_faithful",
            prompt=(
                "What is the current configuration status of the Couchbase MCP "
                "server (read-only mode, connection settings)?"
            ),
            expectation=(
                "Faithfulness check. The answer must reflect the configuration "
                "the tool returned (e.g. read-only mode, whether a connection is "
                "configured). FAIL only if it states configuration values that "
                "contradict the tool output."
            ),
        )
    )

    cases.append(
        ResultCase(
            test_id="connection_faithful",
            prompt=f"Is the connection to bucket '{bucket}' working right now?",
            expectation=(
                "Faithfulness check. The answer must reflect the connection-test "
                "result the tool returned (success or failure). FAIL only if it "
                "reports the opposite of what the tool returned."
            ),
        )
    )

    cases.append(
        ResultCase(
            test_id="cluster_metrics_faithful",
            prompt=(
                "Over the last hour, what has the CPU utilization looked like on "
                "my Couchbase cluster?"
            ),
            expectation=(
                "Faithfulness check. The answer must reflect the stats-range tool "
                "output (whether the requested metric was found, its trend/values, "
                "or any per-metric error the tool reported). FAIL if it invents "
                "numeric values not present in the tool output, or if the tool "
                "reported an error (e.g. connection or metric-not-found) but the "
                "answer claims a successful trend anyway."
            ),
        )
    )

    cases.append(
        ResultCase(
            test_id="nodes_in_cluster_faithful",
            prompt="What nodes are currently part of my Couchbase cluster?",
            expectation=(
                "Faithfulness check. The answer must reflect the node list the "
                "tool returned. FAIL if it invents nodes not present in the tool "
                "output or omits nodes that were returned."
            ),
        )
    )

    return cases


@pytest.fixture()
def server_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


SERVER_RESULT_CASE_IDS = [
    "buckets_lists_test_bucket",
    "scopes_faithful",
    "collections_faithful",
    "scopes_and_collections_faithful",
    "health_faithful",
    "config_faithful",
    "connection_faithful",
    "cluster_metrics_faithful",
    "nodes_in_cluster_faithful",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", SERVER_RESULT_CASE_IDS)
async def test_server_result(
    case_id: str,
    server_cases: list[ResultCase],
    accuracy_client,
    openai_agent,
    judge,
    openai_model: str,
    result_storage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in server_cases if c.test_id == case_id)
    await assert_result_case(
        case,
        accuracy_client=accuracy_client,
        openai_agent=openai_agent,
        judge=judge,
        openai_model=openai_model,
        result_storage=result_storage,
        accuracy_run_id=accuracy_run_id,
        commit_sha=commit_sha,
    )
