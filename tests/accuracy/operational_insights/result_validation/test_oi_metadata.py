"""Result-validation evals for the OI metadata tools (LLM-as-judge).

Covers oi_get_databases_in_cluster, oi_get_scopes_in_database,
oi_get_collections_in_scope and oi_get_schema_for_collection.

Each case seeds its own scope/collection (and, for the schema case,
documents with known fields), so the correct answer is deterministic ground
truth rather than whatever happens to be on the cluster. The judge then
checks the answer actually reflects it.

The hallucination case is the valuable one here: asking about a scope that
does not exist, where inventing plausible collection names is exactly the
failure this tier is meant to catch.

Its rubric scores only that one property, and accepts any honest non-answer
including a bare "I don't know" — matching how the operational tier words its
own no-hallucination cases. That phrasing is not incidental: the agent's
system prompt (see ``accuracy/sdk/agent.py``) explicitly instructs the model
to answer "I don't know" when a request cannot be fulfilled, so a rubric
demanding the words "does not exist" would fail the model for obeying its own
instructions rather than for fabricating anything.
"""

from __future__ import annotations

import pytest

from accuracy.sdk import ResultCase

from .._seeding import drop_scope, seed_collection, unique_name
from ._harness import assert_result_case

_DOCS = [
    {"id": "oiacc-1", "name": "alpha", "rating": 5, "city": "Paris"},
    {"id": "oiacc-2", "name": "beta", "rating": 3, "city": "Berlin"},
    {"id": "oiacc-3", "name": "gamma", "rating": 4, "city": "Madrid"},
]


def _build_cases(database: str, scope: str, collection: str) -> list[ResultCase]:
    seed = seed_collection(database, scope, collection, documents=_DOCS)
    cleanup = drop_scope(database, scope)
    missing_scope = unique_name("oiacc_absent_scope")

    return [
        ResultCase(
            test_id="databases_include_seeded_database",
            prompt="What databases are on this cluster? List their names.",
            expectation=(
                f"The answer must list the database '{database}' among the "
                "databases on the cluster. PASS if it names it (other "
                "databases may also be listed). FAIL if it omits it or claims "
                "the cluster has no databases."
            ),
            seed=seed,
            cleanup=cleanup,
        ),
        ResultCase(
            test_id="scopes_include_seeded_scope",
            prompt=f"List the scopes in the '{database}' database.",
            expectation=(
                f"The answer must include the scope '{scope}'. PASS if that "
                "scope is named (others may also appear). FAIL if it is "
                "missing, or if the answer reports no scopes."
            ),
            seed=seed,
            cleanup=cleanup,
        ),
        ResultCase(
            test_id="collections_include_seeded_collection",
            prompt=(
                f"What collections are in scope '{scope}' of database "
                f"'{database}'?"
            ),
            expectation=(
                f"The answer must name the collection '{collection}'. PASS if "
                "it does. FAIL if it omits it, reports the scope as empty, or "
                "invents collections that were not in the tool output."
            ),
            seed=seed,
            cleanup=cleanup,
        ),
        ResultCase(
            test_id="schema_reflects_seeded_fields",
            prompt=(
                f"What fields do documents in collection '{collection}' "
                f"(scope '{scope}', database '{database}') have?"
            ),
            expectation=(
                "The seeded documents have exactly these fields: id, name, "
                "rating, city. PASS if the answer reports these fields "
                "(naming 'rating' as a numeric/integer type and 'name'/'city' "
                "as strings is correct but not required). FAIL if it omits "
                "most of them or invents fields that do not exist, such as "
                "'price' or 'address'."
            ),
            seed=seed,
            cleanup=cleanup,
        ),
        # No seed: the scope genuinely does not exist.
        ResultCase(
            test_id="missing_scope_no_hallucination",
            prompt=(
                f"What collections are in scope '{missing_scope}' of database "
                f"'{database}'?"
            ),
            expectation=(
                f"The scope '{missing_scope}' does not exist on this cluster, "
                "so there are no collections to report. This checks ONE "
                "property: no hallucination. PASS if the answer avoids "
                "inventing collections — any honest non-answer PASSES ('the "
                "scope does not exist', 'no collections were found', 'could "
                "not retrieve', or 'I don't know'). FAIL ONLY if it names a "
                "collection or describes contents for this scope."
            ),
        ),
    ]


@pytest.fixture()
def metadata_result_cases(oi_database: str):
    scope = unique_name("oiacc_scope")
    collection = unique_name("oiacc_coll")
    return _build_cases(oi_database, scope, collection)


METADATA_RESULT_CASE_IDS = [
    "databases_include_seeded_database",
    "scopes_include_seeded_scope",
    "collections_include_seeded_collection",
    "schema_reflects_seeded_fields",
    "missing_scope_no_hallucination",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", METADATA_RESULT_CASE_IDS)
async def test_oi_metadata_result(
    case_id: str,
    metadata_result_cases: list[ResultCase],
    accuracy_client,
    openai_agent,
    judge,
    openai_model: str,
    result_storage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in metadata_result_cases if c.test_id == case_id)
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
