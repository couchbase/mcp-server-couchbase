"""Result-validation evals for the scope/collection management tools
(LLM-as-judge).

These are faithfulness checks — create/delete success or failure is not fixed
ground truth to recite, so the judge verifies the answer is consistent with
the tool's outcome rather than against seeded field values (as the KV get
cases do). Every case operates on a unique, throwaway scope/collection name
so runs never collide across parallel/repeated runs.
"""

from __future__ import annotations

import pytest

from accuracy.sdk import (
    ResultCase,
    drop_scope,
    seed_collection,
    seed_scope,
    unique_name,
)

from ._harness import assert_result_case


def _build_cases(bucket: str) -> list[ResultCase]:
    cases: list[ResultCase] = []

    # --- create_scope (faithfulness on success report) -----------------
    new_scope = unique_name("rv_scope")
    cases.append(
        ResultCase(
            test_id="create_scope_reports_success",
            prompt=f"Create a new scope named '{new_scope}' in bucket '{bucket}'.",
            expectation=(
                "The scope does not yet exist, so the create succeeds. A "
                "correct answer confirms the scope was created successfully. "
                "It must NOT report failure or claim the scope already existed."
            ),
            cleanup=drop_scope(bucket, new_scope),
        )
    )

    # --- create_scope: already exists -> must not hallucinate success ---
    dup_scope = unique_name("rv_scope")
    cases.append(
        ResultCase(
            test_id="create_scope_already_exists_reports_failure",
            prompt=(f"Create a new scope named '{dup_scope}' in bucket '{bucket}'."),
            expectation=(
                "The scope already exists (it was seeded), so the create "
                "fails with an already-exists error. A correct answer reports "
                "that the creation failed / the scope already exists. FAIL if "
                "the answer claims the scope was newly created."
            ),
            seed=seed_scope(bucket, dup_scope),
            cleanup=drop_scope(bucket, dup_scope),
        )
    )

    # --- create_collection (faithfulness; seed precondition) ------------
    cc_scope = unique_name("rv_scope")
    new_col = unique_name("rv_col")
    cases.append(
        ResultCase(
            test_id="create_collection_reports_success",
            prompt=(
                f"Create a new collection named '{new_col}' in scope "
                f"'{cc_scope}' of bucket '{bucket}'."
            ),
            expectation=(
                "The scope exists and the collection does not, so the create "
                "succeeds. A correct answer confirms the collection was "
                "created successfully. It must NOT report failure."
            ),
            seed=seed_scope(bucket, cc_scope),
            cleanup=drop_scope(bucket, cc_scope),
        )
    )

    # --- delete_scope (faithfulness; seed precondition) ------------------
    del_scope = unique_name("rv_scope")
    cases.append(
        ResultCase(
            test_id="delete_scope_reports_success",
            prompt=f"Delete the scope named '{del_scope}' from bucket '{bucket}'.",
            expectation=(
                "The scope exists (it was seeded), so the delete succeeds. A "
                "correct answer confirms the scope was deleted/removed "
                "successfully. It must NOT report failure or not-found."
            ),
            seed=seed_scope(bucket, del_scope),
            # No-op if the model already dropped it.
            cleanup=drop_scope(bucket, del_scope),
        )
    )

    # --- delete_scope: nonexistent -> must not hallucinate success -------
    missing_scope = unique_name("rv_missing_scope")
    cases.append(
        ResultCase(
            test_id="delete_nonexistent_scope_no_hallucination",
            prompt=(
                f"Delete the scope named '{missing_scope}' from bucket '{bucket}'."
            ),
            expectation=(
                "The scope does not exist, so the tool returns a not-found "
                "error. This checks ONE property: no hallucination. PASS if "
                "the answer avoids claiming the scope was deleted and instead "
                "reports the failure / not-found condition. FAIL ONLY if it "
                "claims the deletion succeeded."
            ),
        )
    )

    # --- delete_collection (faithfulness; seed precondition) -------------
    dc_scope = unique_name("rv_scope")
    dc_col = unique_name("rv_col")
    make_scope = seed_scope(bucket, dc_scope)
    make_collection = seed_collection(bucket, dc_scope, dc_col)

    async def _seed_scope_and_collection(client) -> None:
        await make_scope(client)
        await make_collection(client)

    cases.append(
        ResultCase(
            test_id="delete_collection_reports_success",
            prompt=(
                f"Delete the collection named '{dc_col}' from scope "
                f"'{dc_scope}' in bucket '{bucket}'."
            ),
            expectation=(
                "The collection exists (it was seeded), so the delete "
                "succeeds. A correct answer confirms the collection was "
                "deleted/removed successfully. It must NOT report failure."
            ),
            seed=_seed_scope_and_collection,
            cleanup=drop_scope(bucket, dc_scope),
        )
    )

    return cases


@pytest.fixture()
def collection_management_cases(
    test_bucket: str, test_scope: str, test_collection: str
):
    return _build_cases(test_bucket)


COLLECTION_MANAGEMENT_RESULT_CASE_IDS = [
    "create_scope_reports_success",
    "create_scope_already_exists_reports_failure",
    "create_collection_reports_success",
    "delete_scope_reports_success",
    "delete_nonexistent_scope_no_hallucination",
    "delete_collection_reports_success",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", COLLECTION_MANAGEMENT_RESULT_CASE_IDS)
async def test_collection_management_result(
    case_id: str,
    collection_management_cases: list[ResultCase],
    accuracy_client,
    openai_agent,
    judge,
    openai_model: str,
    result_storage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in collection_management_cases if c.test_id == case_id)
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
