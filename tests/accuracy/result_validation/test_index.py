"""Result-validation evals for the index tools (LLM-as-judge).

Both are faithfulness checks — index state and advisor recommendations are
not seeded ground truth, so the judge verifies the answer is consistent with
the tool output rather than against fixed values.
"""

from __future__ import annotations

import uuid

import pytest

from accuracy.sdk import ResultCase
from accuracy.sdk.client import AccuracyTestingClient
from accuracy.sdk.runner import SetupHook

from ._harness import assert_result_case


def _create_deferred_index(
    bucket: str, scope: str, collection: str, index_name: str
) -> SetupHook:
    """Return a hook that creates a deferred index (silently)."""

    async def _hook(client: AccuracyTestingClient) -> None:
        await client.call_tool_silent(
            "create_index",
            {
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "index_name": index_name,
                "keys": ["email"],
            },
        )

    return _hook


def _drop_index(bucket: str, scope: str, collection: str, index_name: str) -> SetupHook:
    """Return a hook that drops an index (silently, ignoring if already gone)."""

    async def _hook(client: AccuracyTestingClient) -> None:
        await client.call_tool_silent(
            "drop_index",
            {
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "index_name": index_name,
                "ignore_if_not_exists": True,
            },
        )

    return _hook


def _build_cases(bucket: str, scope: str, collection: str) -> list[ResultCase]:
    cases: list[ResultCase] = []

    # NOTE: we ask a single, bounded fact ("is there a primary index?") rather
    # than "list every index". A full-bucket index list can be 20+ entries, and
    # an LLM judge is unreliable at verifying exhaustive set-membership over a
    # large unordered list — it randomly mislabels real entries as fabricated.
    # A single-fact question keeps the faithfulness check reliable while still
    # validating that the agent correctly read the tool's index output.
    cases.append(
        ResultCase(
            test_id="list_indexes_faithful",
            prompt=(
                f"Does bucket '{bucket}' have a primary index? Answer yes or no, "
                "and if yes, name one primary index."
            ),
            expectation=(
                "Faithfulness check on a single fact: does a PRIMARY index exist "
                f"for bucket '{bucket}'? In the tool output a primary index has a "
                "definition like 'CREATE PRIMARY INDEX ...' (or isPrimary=true). "
                "PASS if the answer's yes/no matches the tool output: if the tool "
                "output contains any primary index, the answer must say yes and "
                "name a primary index that actually appears in the output; if it "
                "contains none, the answer must say no. FAIL only if the answer "
                "contradicts the tool output or names a primary index not present "
                "in it. The answer need NOT enumerate every index."
            ),
        )
    )

    cases.append(
        ResultCase(
            test_id="index_advisor_faithful",
            prompt=(
                f"Recommend an index for this query in bucket '{bucket}', scope "
                f"'{scope}': SELECT * FROM `{collection}` WHERE country = 'France'"
            ),
            expectation=(
                "Faithfulness check. The answer must reflect the advisor tool's "
                "output — if the tool recommended one or more indexes, the "
                "answer should convey that recommendation (e.g. a CREATE INDEX "
                "on the relevant field); if the tool returned no recommendation, "
                "the answer should say so. FAIL only if the answer fabricates a "
                "recommendation that contradicts the tool output or invents "
                "results the tool did not return."
            ),
        )
    )

    create_index_name = f"idx_email_result_test_{uuid.uuid4().hex[:8]}"
    cases.append(
        ResultCase(
            test_id="create_index_reports_deferred_and_next_step",
            prompt=(
                f"Create an index named '{create_index_name}' on the 'email' "
                f"field of the '{collection}' collection in scope '{scope}' of "
                f"bucket '{bucket}', then tell me its current status."
            ),
            expectation=(
                "The answer must reflect that the index was created and is in "
                "the deferred/not-yet-built state (the create_index tool "
                "defaults to deferred=True), and should mention that building "
                "it (or checking again later) is needed before it's usable. "
                "FAIL if the answer claims the index is already online/built "
                "immediately after creation, or fabricates a status the tool "
                "did not report."
            ),
            cleanup=_drop_index(bucket, scope, collection, create_index_name),
        )
    )

    build_index_name = f"idx_build_result_test_{uuid.uuid4().hex[:8]}"
    cases.append(
        ResultCase(
            test_id="build_index_reports_via_list_indexes",
            prompt=(
                f"There is a deferred index named '{build_index_name}' on the "
                f"'{collection}' collection in scope '{scope}' of bucket "
                f"'{bucket}'. Build it now and tell me whether it's online yet."
            ),
            expectation=(
                "The answer must be grounded in an actual status check (e.g. "
                "list_indexes) rather than assuming the build finished the "
                "moment build_index was called — the build is asynchronous. "
                "PASS if the answer reports whatever state the tools actually "
                "returned (deferred, building, or online) without claiming "
                "certainty the tools didn't provide. FAIL if the answer "
                "asserts the index is online without having checked, or "
                "invents a status the tool output didn't contain."
            ),
            seed=_create_deferred_index(bucket, scope, collection, build_index_name),
            cleanup=_drop_index(bucket, scope, collection, build_index_name),
        )
    )

    return cases


@pytest.fixture()
def index_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


INDEX_RESULT_CASE_IDS = [
    "list_indexes_faithful",
    "index_advisor_faithful",
    "create_index_reports_deferred_and_next_step",
    "build_index_reports_via_list_indexes",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", INDEX_RESULT_CASE_IDS)
async def test_index_result(
    case_id: str,
    index_cases: list[ResultCase],
    accuracy_client,
    openai_agent,
    judge,
    openai_model: str,
    result_storage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in index_cases if c.test_id == case_id)
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
