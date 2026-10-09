"""Result-validation evals for the OI index tools (LLM-as-judge).

Covers oi_list_indexes and oi_create_index.

Both cases have real ground truth rather than being faithfulness-only: the
seed creates an index with a known name on a known collection, so "what
indexes are on this collection?" has one correct answer. The create case
then verifies the model reports the creation accurately — and, in the
listing that follows, that the new index actually shows up.
"""

from __future__ import annotations

import pytest

from accuracy.sdk import ResultCase

from .._seeding import drop_scope, seed_collection, seed_index, unique_name
from ._harness import assert_result_case

_DOCS = [
    {"id": "oiacc-1", "name": "alpha", "title": "First"},
    {"id": "oiacc-2", "name": "beta", "title": "Second"},
]


def _build_cases(database: str, scope: str, collection: str) -> list[ResultCase]:
    cleanup = drop_scope(database, scope)
    existing_index = unique_name("oiacc_idx")
    base_seed = seed_collection(database, scope, collection, documents=_DOCS)

    async def _seed_with_index(client) -> None:
        await base_seed(client)
        await seed_index(database, scope, collection, existing_index)(client)

    return [
        ResultCase(
            test_id="list_indexes_reports_seeded_index",
            prompt=(
                f"What secondary indexes exist on collection '{collection}' in "
                f"scope '{scope}' of database '{database}'?"
            ),
            expectation=(
                f"An index named '{existing_index}' exists on that collection, "
                "indexing the 'name' field. PASS if the answer names that "
                "index. FAIL if it reports no indexes, or invents an index "
                "name that was not in the tool output."
            ),
            seed=_seed_with_index,
            cleanup=cleanup,
        ),
        ResultCase(
            test_id="create_index_reports_success",
            prompt=(
                f"Create a secondary index called 'idx_title' on the 'title' "
                f"string field of collection '{collection}' in scope '{scope}' "
                f"of database '{database}', then confirm it exists."
            ),
            expectation=(
                "PASS if the answer states the index 'idx_title' was created "
                "successfully, and (if it verified by listing) reports it "
                "among the collection's indexes. FAIL if it claims failure "
                "when the tool reported success, claims success when the tool "
                "reported an error, or describes creating a different index."
            ),
            seed=base_seed,
            cleanup=cleanup,
        ),
        ResultCase(
            test_id="list_indexes_empty_collection_no_hallucination",
            prompt=(
                f"List the secondary indexes on collection '{collection}' in "
                f"scope '{scope}' of database '{database}'."
            ),
            expectation=(
                "This collection has no secondary indexes — only its primary "
                "key, which this tool does not list. This checks ONE "
                "property: no hallucination. PASS if the answer avoids "
                "inventing indexes — any honest non-answer PASSES ('there are "
                "no secondary indexes', 'none found', or 'I don't know'). "
                "FAIL ONLY if it names an index for this collection."
            ),
            seed=base_seed,
            cleanup=cleanup,
        ),
    ]


@pytest.fixture()
def index_result_cases(oi_database: str):
    scope = unique_name("oiacc_scope")
    collection = unique_name("oiacc_coll")
    return _build_cases(oi_database, scope, collection)


INDEX_RESULT_CASE_IDS = [
    "list_indexes_reports_seeded_index",
    "create_index_reports_success",
    "list_indexes_empty_collection_no_hallucination",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", INDEX_RESULT_CASE_IDS)
async def test_oi_index_result(
    case_id: str,
    index_result_cases: list[ResultCase],
    accuracy_client,
    openai_agent,
    judge,
    openai_model: str,
    result_storage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in index_result_cases if c.test_id == case_id)
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
