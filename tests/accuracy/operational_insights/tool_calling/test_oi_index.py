"""Accuracy tests for the Operational Insights index tools.

Covers:
  - oi_list_indexes (cluster-wide and filtered)
  - oi_create_index (scalar, composite, and the array/UNNEST form)

``oi_create_index`` has the most structured input of any OI tool: ``fields``
is a list of index elements whose shape changes with what is being indexed —
``{"name", "type"}`` for a scalar, ``{"unnest", "type"}`` or
``{"unnest", "select"}`` for an array. The array form additionally *requires*
``exclude_unknown_key=True``; the server rejects it otherwise. That coupling
between two separate arguments is exactly the kind of thing a model gets
wrong, so the array case asserts it explicitly.

``fields`` is matched with predicates rather than literal equality: the type
annotation on a field is optional in the schema, so a model may legitimately
pass ``[{"name": "title"}]`` or ``[{"name": "title", "type": "string"}]``.
What matters is the field name and, for arrays, the unnest/exclude pairing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from accuracy.sdk import (
    AccuracyCase,
    DiskResultStorage,
    Matcher,
    OpenAIAgent,
    run_accuracy_case,
)
from accuracy.sdk.types import ExpectedToolCall

from .._seeding import drop_scope, seed_collection, seed_index, unique_name

_DOCS = [
    {"id": "oiacc-1", "name": "alpha", "title": "First", "public_likes": ["ann"]},
    {"id": "oiacc-2", "name": "beta", "title": "Second", "public_likes": ["bob"]},
]


def _names_field(*expected: str) -> Matcher:
    """Match a ``fields`` list naming exactly ``expected``, in order.

    Tolerates the optional ``type`` key being present or absent, which the
    schema allows for scalar fields.
    """

    def _ok(value: Any) -> bool:
        if not isinstance(value, list) or len(value) != len(expected):
            return False
        return all(
            isinstance(element, dict)
            and str(element.get("name", "")).lower() == want.lower()
            for element, want in zip(value, expected, strict=True)
        )

    return _Predicate(_ok)


def _unnest_field(path: str) -> Matcher:
    """Match a ``fields`` list holding one UNNEST element over ``path``."""

    def _ok(value: Any) -> bool:
        if not isinstance(value, list) or len(value) != 1:
            return False
        element = value[0]
        if not isinstance(element, dict):
            return False
        unnest = element.get("unnest")
        # The schema accepts a bare string or a list for nested arrays.
        if isinstance(unnest, list):
            unnest = ".".join(str(part) for part in unnest)
        return str(unnest or "").lower() == path.lower()

    return _Predicate(_ok)


class _Predicate(Matcher):
    """Adapt an arbitrary value predicate to the Matcher interface.

    ``Matcher`` ships ``string``/``number``/``boolean`` predicates but nothing
    for a list-of-dicts argument like ``fields``, so this fills that gap
    rather than forcing brittle literal equality on a structure with optional
    keys.
    """

    def __init__(self, predicate) -> None:
        self._predicate = predicate

    def match(self, actual: Any) -> float:
        try:
            return 1.0 if self._predicate(actual) else 0.0
        except Exception:
            return 0.0


def _build_cases(database: str, scope: str, collection: str) -> list[AccuracyCase]:
    seed = seed_collection(database, scope, collection, documents=_DOCS)
    cleanup = drop_scope(database, scope)
    existing_index = unique_name("oiacc_idx")

    async def _seed_with_index(client) -> None:
        await seed(client)
        await seed_index(database, scope, collection, existing_index)(client)

    return [
        AccuracyCase(
            test_id="oi_list_indexes_cluster_wide",
            prompt="List every secondary index on this cluster.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_list_indexes",
                    # All three filters are optional and should be omitted for
                    # a cluster-wide listing.
                    parameters={
                        "database_name": Matcher.any_of(
                            Matcher.undefined(), Matcher.null()
                        ),
                        "scope_name": Matcher.any_of(
                            Matcher.undefined(), Matcher.null()
                        ),
                        "collection_name": Matcher.any_of(
                            Matcher.undefined(), Matcher.null()
                        ),
                    },
                ),
            ],
            seed=_seed_with_index,
            cleanup=cleanup,
        ),
        AccuracyCase(
            test_id="oi_list_indexes_for_collection",
            prompt=(
                f"What indexes exist on the '{collection}' collection in scope "
                f"'{scope}' of database '{database}'?"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_list_indexes",
                    parameters={
                        "database_name": database,
                        "scope_name": scope,
                        "collection_name": collection,
                    },
                ),
            ],
            seed=_seed_with_index,
            cleanup=cleanup,
        ),
        AccuracyCase(
            test_id="oi_create_index_scalar",
            prompt=(
                f"Create a secondary index named 'idx_title' on the 'title' "
                f"field (a string) of collection '{collection}' in scope "
                f"'{scope}' of database '{database}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_create_index",
                    parameters={
                        "database_name": database,
                        "scope_name": scope,
                        "collection_name": collection,
                        "index_name": "idx_title",
                        "fields": _names_field("title"),
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        ),
        AccuracyCase(
            test_id="oi_create_index_composite",
            prompt=(
                f"Create a composite index named 'idx_name_title' on collection "
                f"'{collection}' in scope '{scope}' of database '{database}', "
                "indexing 'name' then 'title', both strings."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_create_index",
                    parameters={
                        "database_name": database,
                        "scope_name": scope,
                        "collection_name": collection,
                        "index_name": "idx_name_title",
                        "fields": _names_field("name", "title"),
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        ),
        # The coupled-argument case: an array index is invalid without
        # exclude_unknown_key=True, and the prompt deliberately does not say so.
        AccuracyCase(
            test_id="oi_create_index_array_unnest",
            prompt=(
                f"Create an index named 'idx_likes' on collection '{collection}' "
                f"in scope '{scope}' of database '{database}' that indexes the "
                "values inside the 'public_likes' array, which holds strings."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_create_index",
                    parameters={
                        "database_name": database,
                        "scope_name": scope,
                        "collection_name": collection,
                        "index_name": "idx_likes",
                        "fields": _unnest_field("public_likes"),
                        "exclude_unknown_key": True,
                    },
                ),
            ],
            seed=seed,
            cleanup=cleanup,
        ),
    ]


@pytest.fixture()
def index_cases(oi_database: str):
    scope = unique_name("oiacc_scope")
    collection = unique_name("oiacc_coll")
    return _build_cases(oi_database, scope, collection)


INDEX_CASE_IDS = [
    "oi_list_indexes_cluster_wide",
    "oi_list_indexes_for_collection",
    "oi_create_index_scalar",
    "oi_create_index_composite",
    "oi_create_index_array_unnest",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", INDEX_CASE_IDS)
async def test_oi_index_tool_accuracy(
    case_id: str,
    index_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in index_cases if c.test_id == case_id)
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
