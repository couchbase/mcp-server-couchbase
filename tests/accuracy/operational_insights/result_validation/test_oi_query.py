"""Result-validation evals for the OI query tools (LLM-as-judge).

Covers oi_run_query_sync, oi_explain_query, and the async lifecycle as the
model experiences it end-to-end (start a query, poll it, report the rows).

Seeded data gives these cases real ground truth: the collection holds three
documents with known ratings, so "how many?" and "which has the highest
rating?" have exactly one correct answer and a wrong number is a hard fail —
unlike the faithfulness-only checks used where results are non-deterministic.

The async case is deliberately scored on the *final answer* rather than on
which tools were called. Whether the model polls once or three times before
the query finishes is not a correctness question; whether it eventually
reports the right rows is.
"""

from __future__ import annotations

import pytest

from accuracy.sdk import ResultCase

from .._seeding import drop_scope, seed_collection, unique_name
from ._harness import assert_result_case

_DOCS = [
    {"id": "oiacc-1", "name": "alpha", "rating": 5},
    {"id": "oiacc-2", "name": "beta", "rating": 3},
    {"id": "oiacc-3", "name": "gamma", "rating": 4},
]


def _build_cases(database: str, scope: str, collection: str) -> list[ResultCase]:
    ks = f"`{database}`.`{scope}`.`{collection}`"
    seed = seed_collection(database, scope, collection, documents=_DOCS)
    cleanup = drop_scope(database, scope)

    return [
        ResultCase(
            test_id="sync_query_reports_seeded_count",
            prompt=(
                f"How many documents are in collection '{collection}' in scope "
                f"'{scope}' of database '{database}'?"
            ),
            expectation=(
                "The collection holds exactly 3 documents. PASS only if the "
                "answer reports 3. FAIL on any other number."
            ),
            seed=seed,
            cleanup=cleanup,
        ),
        ResultCase(
            test_id="sync_query_reports_seeded_max",
            prompt=(
                f"Which document in {ks} has the highest rating, and what is "
                "that rating?"
            ),
            expectation=(
                "The seeded documents are alpha (rating 5), beta (3) and "
                "gamma (4). PASS only if the answer identifies 'alpha' as the "
                "highest, with rating 5. FAIL if it names a different "
                "document or a different rating."
            ),
            seed=seed,
            cleanup=cleanup,
        ),
        ResultCase(
            test_id="sync_query_filtered_rows",
            prompt=(
                f"Which documents in {ks} have a rating of 4 or more? Give me "
                "their names."
            ),
            expectation=(
                "The matching documents are alpha (rating 5) and gamma "
                "(rating 4). PASS if the answer names both and does not "
                "include beta. FAIL if it omits either, includes beta, or "
                "invents a name."
            ),
            seed=seed,
            cleanup=cleanup,
        ),
        ResultCase(
            test_id="explain_plan_is_faithful",
            prompt=(
                f"Show me the query execution plan for SELECT * FROM {ks} "
                "WHERE rating > 3. Do not run the query itself."
            ),
            expectation=(
                "Faithfulness check. PASS if the answer describes a query "
                "plan grounded in the tool output — it should discuss plan "
                "structure (scans, operators, estimated work) rather than "
                "returning document rows. PASS is still correct if the plan "
                "is simple or rule-based. FAIL if the answer reports actual "
                "document data as though it had executed the query, or "
                "describes plan operators absent from the tool output."
            ),
            seed=seed,
            cleanup=cleanup,
        ),
        ResultCase(
            test_id="async_roundtrip_reports_rows",
            prompt=(
                f"Run a query over {ks} in the background rather than "
                "blocking: select every document's name and rating. Wait for "
                "it to finish, then tell me the results."
            ),
            expectation=(
                "The collection holds alpha (rating 5), beta (3) and gamma "
                "(4). PASS if the answer reports all three names with their "
                "correct ratings. It is also acceptable to PASS if the answer "
                "states the query was still running and gives the handle to "
                "check later. FAIL if it reports wrong ratings, omits "
                "documents while claiming the results are complete, or "
                "fabricates rows."
            ),
            seed=seed,
            cleanup=cleanup,
        ),
    ]


@pytest.fixture()
def query_result_cases(oi_database: str):
    scope = unique_name("oiacc_scope")
    collection = unique_name("oiacc_coll")
    return _build_cases(oi_database, scope, collection)


QUERY_RESULT_CASE_IDS = [
    "sync_query_reports_seeded_count",
    "sync_query_reports_seeded_max",
    "sync_query_filtered_rows",
    "explain_plan_is_faithful",
    "async_roundtrip_reports_rows",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", QUERY_RESULT_CASE_IDS)
async def test_oi_query_result(
    case_id: str,
    query_result_cases: list[ResultCase],
    accuracy_client,
    openai_agent,
    judge,
    openai_model: str,
    result_storage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in query_result_cases if c.test_id == case_id)
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
