"""Accuracy tests for the index tools.

Covers:
  - list_indexes (all variants: cluster-wide, per-bucket, per-scope,
    per-collection)
  - get_index_advisor_recommendations
  - create_index
  - build_index
  - drop_index
"""

from __future__ import annotations

import json
import uuid

import pytest

from accuracy.sdk import (
    AccuracyCase,
    DiskResultStorage,
    Matcher,
    OpenAIAgent,
    run_accuracy_case,
)
from accuracy.sdk.client import AccuracyTestingClient
from accuracy.sdk.runner import SetupHook
from accuracy.sdk.types import ExpectedToolCall


def _optional() -> Matcher:
    return Matcher.any_of(Matcher.undefined(), Matcher.null())


def _contains(*needles: str) -> Matcher:
    lowered = [n.lower() for n in needles]
    return Matcher.string(lambda value: all(n in value.lower() for n in lowered))


def _create_index(
    bucket: str, scope: str, collection: str, index_name: str
) -> SetupHook:
    """Return a hook that creates a (deferred) index silently, so a case acting
    on an existing index (build/drop) is self-contained and doesn't depend on
    the create case having run first."""

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
    """Return a hook that drops an index silently (ignoring if already gone), so
    a case leaves no index behind whether or not the model dropped it."""

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


def _build_cases(bucket: str, scope: str, collection: str) -> list[AccuracyCase]:
    cases: list[AccuracyCase] = []

    cases.append(
        AccuracyCase(
            test_id="list_indexes_all",
            prompt="List every index in the entire Couchbase cluster.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="list_indexes",
                    parameters={
                        "bucket_name": _optional(),
                        "scope_name": _optional(),
                        "collection_name": _optional(),
                        "index_name": _optional(),
                        "return_raw_index_stats": Matcher.any_of(
                            Matcher.undefined(),
                            Matcher.boolean(False),
                        ),
                    },
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="list_indexes_bucket_filter",
            prompt=f"List the indexes that exist on bucket '{bucket}'.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="list_indexes",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": _optional(),
                        "collection_name": _optional(),
                        "index_name": _optional(),
                        "return_raw_index_stats": Matcher.any_of(
                            Matcher.undefined(),
                            Matcher.boolean(False),
                        ),
                    },
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="list_indexes_collection_filter",
            prompt=(
                f"Show the indexes defined on the '{collection}' collection in "
                f"scope '{scope}' of bucket '{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="list_indexes",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "index_name": _optional(),
                        "return_raw_index_stats": Matcher.any_of(
                            Matcher.undefined(),
                            Matcher.boolean(False),
                        ),
                    },
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="get_index_advisor_recommendations",
            prompt=(
                f"Recommend optimal indexes for this query in bucket '{bucket}', "
                f"scope '{scope}': SELECT * FROM `{collection}` WHERE name = 'test'"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_index_advisor_recommendations",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "query": _contains("select", collection, "name"),
                    },
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="conversational_what_indexes_exist",
            prompt="What indexes does my Couchbase cluster currently have?",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="list_indexes",
                    parameters=Matcher.any_value(),
                ),
            ],
        )
    )

    cases.append(
        AccuracyCase(
            test_id="conversational_make_this_faster",
            prompt=(
                f"This query feels slow — what index should I create to make it "
                f"faster? Use bucket '{bucket}', scope '{scope}'. Query: "
                f"SELECT * FROM `{collection}` WHERE country = 'US'"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_index_advisor_recommendations",
                    parameters=Matcher.any_value(),
                ),
            ],
        )
    )

    create_index_name = f"idx_email_{uuid.uuid4().hex[:8]}"
    cases.append(
        AccuracyCase(
            test_id="create_index_on_field",
            prompt=(
                f"Create an index on the 'email' field for the '{collection}' "
                f"collection in scope '{scope}' of bucket '{bucket}'. Call the "
                f"index '{create_index_name}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="create_index",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "index_name": _contains("email"),
                        "keys": Matcher.any_value(),
                    },
                ),
            ],
            # The model creates it; drop it afterward so the case leaves no state.
            cleanup=_drop_index(bucket, scope, collection, create_index_name),
        )
    )

    build_index_name = f"idx_build_{uuid.uuid4().hex[:8]}"
    cases.append(
        AccuracyCase(
            test_id="build_deferred_indexes",
            prompt=(
                f"Build the deferred indexes on the '{collection}' collection "
                f"in scope '{scope}' of bucket '{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="build_index",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                    },
                ),
            ],
            # Seed a deferred index so there is something to build — independent
            # of the create case.
            seed=_create_index(bucket, scope, collection, build_index_name),
            cleanup=_drop_index(bucket, scope, collection, build_index_name),
        )
    )

    drop_index_name = f"idx_drop_{uuid.uuid4().hex[:8]}"
    cases.append(
        AccuracyCase(
            test_id="drop_index_by_name",
            prompt=(
                f"Drop the index named '{drop_index_name}' on the '{collection}' "
                f"collection in scope '{scope}' of bucket '{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="drop_index",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "index_name": drop_index_name,
                    },
                ),
            ],
            # Seed the index so there is something to drop; cleanup is a no-op
            # once the model has dropped it (ignore_if_not_exists).
            seed=_create_index(bucket, scope, collection, drop_index_name),
            cleanup=_drop_index(bucket, scope, collection, drop_index_name),
        )
    )

    return cases


@pytest.fixture()
def index_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


INDEX_CASE_IDS = [
    "list_indexes_all",
    "list_indexes_bucket_filter",
    "list_indexes_collection_filter",
    "get_index_advisor_recommendations",
    "conversational_what_indexes_exist",
    "conversational_make_this_faster",
    "create_index_on_field",
    "build_deferred_indexes",
    "drop_index_by_name",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", INDEX_CASE_IDS)
async def test_index_tool_accuracy(
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
