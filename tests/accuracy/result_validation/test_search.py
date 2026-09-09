"""Result-validation evals for the FTS/Search tools (LLM-as-judge).

Faithfulness checks — index state and query results are not fixed ground
truth, so the judge verifies the answer is consistent with the tool output
rather than against pre-seeded fixed values.
"""

from __future__ import annotations

import uuid

import pytest

from accuracy.sdk import ResultCase, drop_search_index, seed_search_index

from ._harness import assert_result_case


def _build_cases(bucket: str, scope: str, collection: str) -> list[ResultCase]:
    cases: list[ResultCase] = []
    index_name = f"res_fts_idx_{uuid.uuid4().hex[:8]}"
    seed = seed_search_index(bucket, scope, collection, index_name)
    cleanup = drop_search_index(bucket, scope, index_name)

    cases.append(
        ResultCase(
            test_id="list_search_indexes_by_index_name_faithful",
            prompt=(
                f"What is the idx_type (index type) of the Search index "
                f"'{index_name}' in scope '{scope}' of bucket '{bucket}'?"
            ),
            expectation=(
                "Faithfulness check on a single fact: the seeded index's "
                "idx_type is 'fulltext-index'. PASS if the answer states "
                "idx_type is fulltext-index (matching the tool output). FAIL "
                "if the answer names a different idx_type or fabricates a "
                "value not present in the tool output."
            ),
            seed=seed,
            cleanup=cleanup,
        )
    )

    cases.append(
        ResultCase(
            test_id="run_fts_query_faithful",
            prompt=(
                f"Run a match_all Search query against the index "
                f"'{index_name}' in scope '{scope}' of bucket '{bucket}' and "
                f"tell me how many hits it returned."
            ),
            expectation=(
                "Faithfulness check: the answer must report the hit count "
                "(total_hits) that the run_fts_query tool actually returned — "
                "it may legitimately be zero if the collection has no "
                "indexed documents. FAIL if the answer states a hit count "
                "that contradicts the tool output, or claims a specific "
                "number without having called the tool."
            ),
            seed=seed,
            cleanup=cleanup,
        )
    )

    cases.append(
        ResultCase(
            test_id="run_fts_query_explain_faithful",
            prompt=(
                f"Explain the execution plan for a match_all query against "
                f"the Search index '{index_name}' in scope '{scope}' of "
                f"bucket '{bucket}'."
            ),
            expectation=(
                "Faithfulness check: the answer should describe that an "
                "execution plan/explanation was retrieved for the query "
                "(e.g. mention the explain output), consistent with "
                "run_fts_query's explain=True output. FAIL if the answer "
                "fabricates plan details not present in the tool output, or "
                "claims no explanation is available when the tool actually "
                "returned one."
            ),
            seed=seed,
            cleanup=cleanup,
        )
    )

    return cases


@pytest.fixture()
def search_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


SEARCH_RESULT_CASE_IDS = [
    "list_search_indexes_by_index_name_faithful",
    "run_fts_query_faithful",
    "run_fts_query_explain_faithful",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", SEARCH_RESULT_CASE_IDS)
async def test_search_result(
    case_id: str,
    search_cases: list[ResultCase],
    accuracy_client,
    openai_agent,
    judge,
    openai_model: str,
    result_storage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in search_cases if c.test_id == case_id)
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
