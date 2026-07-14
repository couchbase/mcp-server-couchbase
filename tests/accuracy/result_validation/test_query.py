"""Result-validation evals for the SQL++ query tools (LLM-as-judge).

- run_sql_plus_plus_query: seeded ground truth via a USE KEYS lookup
  (index-free, immediately consistent) so the returned value is exact.
- get_schema_for_collection: faithfulness — INFER samples documents, so we
  seed a doc and check the answer reflects fields actually returned.
- explain_sql_plus_plus_query: faithfulness — the answer must reflect the
  plan the tool returned and not invent plan details.
"""

from __future__ import annotations

import pytest

from accuracy.sdk import ResultCase, delete_document, doc_id, seed_document

from ._harness import assert_result_case


def _build_cases(bucket: str, scope: str, collection: str) -> list[ResultCase]:
    cases: list[ResultCase] = []

    # --- run_sql via USE KEYS (seeded, exact) ---------------------------
    q_id = doc_id("rv_query_usekeys")
    cases.append(
        ResultCase(
            test_id="run_sql_use_keys_value",
            prompt=(
                f"Run a SQL++ query against bucket '{bucket}', scope '{scope}' "
                f"that fetches the document with key '{q_id}' from collection "
                f"'{collection}' using the USE KEYS clause, and tell me the value "
                "of its 'callsign' field."
            ),
            expectation=(
                "The answer must state the callsign is 'KAERMORHEN'. Any other "
                "value, or no value, is incorrect."
            ),
            seed=seed_document(
                bucket,
                scope,
                collection,
                q_id,
                {"name": "Kaer Morhen Air", "callsign": "KAERMORHEN"},
            ),
            cleanup=delete_document(bucket, scope, collection, q_id),
        )
    )

    # --- run_sql filter by a seeded marker (USE KEYS keeps it consistent)
    marker_id = doc_id("rv_query_marker")
    cases.append(
        ResultCase(
            test_id="run_sql_reports_seeded_field",
            prompt=(
                f"Using a SQL++ query on bucket '{bucket}', scope '{scope}', "
                f"fetch the FULL document '{marker_id}' from collection "
                f"'{collection}' with SELECT * ... USE KEYS. Then report the "
                "values of its 'country' field and its 'founded' field exactly "
                "as stored."
            ),
            expectation=(
                "The answer must report the 'country' field value as 'Cintra' "
                "and the 'founded' field value as 1248. Both must be present "
                "and correct."
            ),
            seed=seed_document(
                bucket,
                scope,
                collection,
                marker_id,
                {"name": "Cintra Airways", "country": "Cintra", "founded": 1248},
            ),
            cleanup=delete_document(bucket, scope, collection, marker_id),
        )
    )

    # --- get_schema_for_collection (faithfulness) -----------------------
    schema_id = doc_id("rv_schema")
    cases.append(
        ResultCase(
            test_id="schema_reflects_fields",
            prompt=(
                f"What fields do documents in collection '{collection}' (scope "
                f"'{scope}', bucket '{bucket}') have? Infer the schema."
            ),
            expectation=(
                "Faithfulness check. The answer must describe a schema/field "
                "list that is consistent with the schema the tool returned — it "
                "must not invent fields that the tool output does not contain. "
                "It is acceptable for the answer to list a subset of fields. "
                "INFER may return more than one schema flavor/sample, and the "
                "output includes a '~meta' wrapper; an answer that groups fields "
                "across those samples, or that reports '~meta.id' (the string "
                "document key) separately from a top-level 'id' field (whose own "
                "type comes from the documents), is FAITHFUL — do not penalize "
                "either as an error. FAIL only if it fabricates fields absent "
                "from the tool output or claims it could not infer anything when "
                "the tool returned a schema."
            ),
            # Seed a doc so the collection is non-empty and INFER has a sample.
            seed=seed_document(
                bucket,
                scope,
                collection,
                schema_id,
                {
                    "id": 19999,
                    "type": "airline",
                    "name": "Schema Sample Air",
                    "country": "United States",
                    "iata": "SS",
                    "icao": "SSA",
                    "callsign": "SAMPLE",
                },
            ),
            cleanup=delete_document(bucket, scope, collection, schema_id),
        )
    )

    # --- explain (faithfulness) -----------------------------------------
    explain_id = doc_id("rv_explain")
    cases.append(
        ResultCase(
            test_id="explain_reflects_plan",
            prompt=(
                f"Show me the execution plan for SELECT * FROM `{collection}` "
                f"USE KEYS '{explain_id}' in bucket '{bucket}', scope '{scope}'. "
                "Do not run the query — just explain it."
            ),
            expectation=(
                "Faithfulness check. The answer must describe the query plan "
                "consistent with what the explain tool returned (for a USE KEYS "
                "query this is a key/primary-scan style plan). FAIL only if the "
                "answer fabricates plan details that contradict the tool output, "
                "or claims no plan was produced when the tool returned one."
            ),
            seed=seed_document(
                bucket,
                scope,
                collection,
                explain_id,
                {"name": "Explain Sample Air"},
            ),
            cleanup=delete_document(bucket, scope, collection, explain_id),
        )
    )

    return cases


@pytest.fixture()
def query_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


QUERY_RESULT_CASE_IDS = [
    "run_sql_use_keys_value",
    "run_sql_reports_seeded_field",
    "schema_reflects_fields",
    "explain_reflects_plan",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", QUERY_RESULT_CASE_IDS)
async def test_query_result(
    case_id: str,
    query_cases: list[ResultCase],
    accuracy_client,
    openai_agent,
    judge,
    openai_model: str,
    result_storage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in query_cases if c.test_id == case_id)
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
