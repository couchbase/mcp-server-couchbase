"""Result-validation evals for the KV tools (LLM-as-judge).

Reads (get) use seeded ground truth — the answer must reflect exact seeded
values. Writes (insert/upsert/replace/delete) use faithfulness — the tool
returns success/failure and the answer must report that outcome correctly
without fabricating data. A non-existent get must not hallucinate.

Seeded values use invented tokens (e.g. country 'Zubrowka') so an answer that
didn't actually read the document can't fabricate them.
"""

from __future__ import annotations

import pytest

from accuracy.sdk import ResultCase, delete_document, doc_id, seed_document

from ._harness import assert_result_case


def _build_cases(bucket: str, scope: str, collection: str) -> list[ResultCase]:
    cases: list[ResultCase] = []

    # --- get: exact field lookup (seeded) -------------------------------
    field_id = doc_id("rv_get_field")
    cases.append(
        ResultCase(
            test_id="get_field_lookup",
            prompt=(
                f"In bucket '{bucket}', scope '{scope}', collection "
                f"'{collection}', look up document '{field_id}' and tell me its "
                "country and IATA code."
            ),
            expectation=(
                "The answer must state the country is 'Zubrowka' and the IATA "
                "code is 'ZB'. Both must be present and correct."
            ),
            seed=seed_document(
                bucket,
                scope,
                collection,
                field_id,
                {"name": "Grand Budapest Air", "country": "Zubrowka", "iata": "ZB"},
            ),
            cleanup=delete_document(bucket, scope, collection, field_id),
        )
    )

    # --- get: summarize a richer document (seeded) ----------------------
    summary_id = doc_id("rv_get_summary")
    cases.append(
        ResultCase(
            test_id="get_summarize",
            prompt=(
                f"Fetch document '{summary_id}' from bucket '{bucket}', scope "
                f"'{scope}', collection '{collection}' and summarize what it "
                "describes."
            ),
            expectation=(
                "The answer must convey that this is an airline named 'Novigrad "
                "Wings', based in the city of Novigrad, founded in 1987. A "
                "correct summary mentions the airline name plus at least one of "
                "the city Novigrad or the year 1987, and contradicts none of "
                "them."
            ),
            seed=seed_document(
                bucket,
                scope,
                collection,
                summary_id,
                {
                    "type": "airline",
                    "name": "Novigrad Wings",
                    "city": "Novigrad",
                    "founded": 1987,
                    "active": True,
                },
            ),
            cleanup=delete_document(bucket, scope, collection, summary_id),
        )
    )

    # --- get: numeric question (seeded) ---------------------------------
    num_id = doc_id("rv_get_num")
    cases.append(
        ResultCase(
            test_id="get_numeric_question",
            prompt=(
                f"How many aircraft are in the fleet of document '{num_id}' in "
                f"bucket '{bucket}', scope '{scope}', collection '{collection}'?"
            ),
            expectation=(
                "The answer must state the fleet size is 137 (the 'fleet_size' "
                "field). A different number, or no number, is incorrect."
            ),
            seed=seed_document(
                bucket,
                scope,
                collection,
                num_id,
                {"name": "Redania Regional", "fleet_size": 137},
            ),
            cleanup=delete_document(bucket, scope, collection, num_id),
        )
    )

    # --- get: non-existent -> must NOT hallucinate ----------------------
    missing_id = doc_id("rv_get_missing")
    cases.append(
        ResultCase(
            test_id="get_nonexistent_no_hallucination",
            prompt=(
                f"Get document '{missing_id}' from bucket '{bucket}', scope "
                f"'{scope}', collection '{collection}' and tell me its contents."
            ),
            expectation=(
                "The document does not exist, so the tool returns a not-found "
                "error. This checks ONE property: no hallucination. PASS if the "
                "answer avoids inventing data — it states no document field "
                "values and does not claim the document exists. Any honest "
                "non-answer PASSES ('not found', 'does not exist', 'could not "
                "retrieve', or 'I don't know'). FAIL ONLY if it fabricates "
                "contents or asserts the document exists."
            ),
        )
    )

    # --- insert (faithfulness on success report) ------------------------
    insert_id = doc_id("rv_insert")
    cases.append(
        ResultCase(
            test_id="insert_reports_success",
            prompt=(
                f"Insert a NEW document with id '{insert_id}' into bucket "
                f"'{bucket}', scope '{scope}', collection '{collection}' with "
                'body {"name": "Inserted Air", "active": true}. Fail if it '
                "already exists; do not upsert."
            ),
            expectation=(
                "The insert succeeds (the tool returns success). A correct "
                "answer confirms the document was inserted/created successfully. "
                "It must NOT report failure and must NOT claim the document "
                "already existed."
            ),
            cleanup=delete_document(bucket, scope, collection, insert_id),
        )
    )

    # --- upsert (faithfulness) ------------------------------------------
    upsert_id = doc_id("rv_upsert")
    cases.append(
        ResultCase(
            test_id="upsert_reports_success",
            prompt=(
                f"Upsert document '{upsert_id}' in bucket '{bucket}', scope "
                f"'{scope}', collection '{collection}' with body "
                '{"name": "Upserted Air", "version": 2}.'
            ),
            expectation=(
                "The upsert succeeds. A correct answer confirms the document was "
                "saved/updated successfully. It must NOT report failure."
            ),
            cleanup=delete_document(bucket, scope, collection, upsert_id),
        )
    )

    # --- replace (faithfulness; seed precondition) ----------------------
    replace_id = doc_id("rv_replace")
    cases.append(
        ResultCase(
            test_id="replace_reports_success",
            prompt=(
                f"Replace the existing document '{replace_id}' in bucket "
                f"'{bucket}', scope '{scope}', collection '{collection}' with "
                'body {"name": "Replaced Air", "version": 9}. Fail if it does '
                "not exist; do not upsert."
            ),
            expectation=(
                "The document exists (it was seeded), so the replace succeeds. "
                "A correct answer confirms the document was replaced/updated "
                "successfully. It must NOT report failure or not-found."
            ),
            seed=seed_document(
                bucket,
                scope,
                collection,
                replace_id,
                {"name": "Original Air", "version": 1},
            ),
            cleanup=delete_document(bucket, scope, collection, replace_id),
        )
    )

    # --- delete (faithfulness; seed precondition) -----------------------
    delete_id = doc_id("rv_delete")
    cases.append(
        ResultCase(
            test_id="delete_reports_success",
            prompt=(
                f"Delete document '{delete_id}' from bucket '{bucket}', scope "
                f"'{scope}', collection '{collection}'."
            ),
            expectation=(
                "The document exists (it was seeded), so the delete succeeds. "
                "A correct answer confirms the document was deleted/removed "
                "successfully. It must NOT report failure."
            ),
            seed=seed_document(
                bucket, scope, collection, delete_id, {"name": "Doomed Air"}
            ),
        )
    )

    return cases


@pytest.fixture()
def kv_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


KV_RESULT_CASE_IDS = [
    "get_field_lookup",
    "get_summarize",
    "get_numeric_question",
    "get_nonexistent_no_hallucination",
    "insert_reports_success",
    "upsert_reports_success",
    "replace_reports_success",
    "delete_reports_success",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", KV_RESULT_CASE_IDS)
async def test_kv_result(
    case_id: str,
    kv_cases: list[ResultCase],
    accuracy_client,
    openai_agent,
    judge,
    openai_model: str,
    result_storage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in kv_cases if c.test_id == case_id)
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
