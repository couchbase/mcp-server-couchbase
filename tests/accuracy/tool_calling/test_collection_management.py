"""Accuracy tests for the scope/collection management tools.

Cases:
  - create_scope / create_collection / delete_scope / delete_collection
    (one each, with seed/cleanup against a throwaway scope)
  - a conversational phrasing that must still route to create_scope

Every case operates on a unique, throwaway scope name so runs never touch the
shared test scope/collection and never collide across parallel/repeated runs.
"""

from __future__ import annotations

import json

import pytest

from accuracy.sdk import (
    AccuracyCase,
    DiskResultStorage,
    Matcher,
    OpenAIAgent,
    drop_scope,
    run_accuracy_case,
    seed_collection,
    seed_scope,
    unique_name,
)
from accuracy.sdk.runner import SetupHook
from accuracy.sdk.types import ExpectedToolCall


def _seed_scope_and_collection(bucket: str, scope: str, collection: str) -> SetupHook:
    """Seed both a scope and a collection within it (for delete_collection)."""
    make_scope = seed_scope(bucket, scope)
    make_collection = seed_collection(bucket, scope, collection)

    async def _hook(client) -> None:
        await make_scope(client)
        await make_collection(client)

    return _hook


def _build_cases(bucket: str) -> list[AccuracyCase]:
    cases: list[AccuracyCase] = []

    new_scope = unique_name("acc_scope")
    cases.append(
        AccuracyCase(
            test_id="create_scope",
            prompt=f"Create a new scope named '{new_scope}' in bucket '{bucket}'.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="create_scope",
                    parameters={"bucket_name": bucket, "scope_name": new_scope},
                ),
            ],
            cleanup=drop_scope(bucket, new_scope),
        )
    )

    cc_scope = unique_name("acc_scope")
    new_col = unique_name("acc_col")
    cases.append(
        AccuracyCase(
            test_id="create_collection",
            prompt=(
                f"Create a new collection named '{new_col}' in scope '{cc_scope}' "
                f"of bucket '{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="create_collection",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": cc_scope,
                        "collection_name": new_col,
                    },
                ),
            ],
            seed=seed_scope(bucket, cc_scope),
            cleanup=drop_scope(bucket, cc_scope),
        )
    )

    del_scope = unique_name("acc_scope")
    cases.append(
        AccuracyCase(
            test_id="delete_scope",
            prompt=f"Delete the scope named '{del_scope}' from bucket '{bucket}'.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="delete_scope",
                    parameters={"bucket_name": bucket, "scope_name": del_scope},
                ),
            ],
            seed=seed_scope(bucket, del_scope),
            # No-op if the model already dropped it; cascades otherwise.
            cleanup=drop_scope(bucket, del_scope),
        )
    )

    dc_scope = unique_name("acc_scope")
    dc_col = unique_name("acc_col")
    cases.append(
        AccuracyCase(
            test_id="delete_collection",
            prompt=(
                f"Delete the collection named '{dc_col}' in scope '{dc_scope}' "
                f"of bucket '{bucket}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="delete_collection",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": dc_scope,
                        "collection_name": dc_col,
                    },
                ),
            ],
            seed=_seed_scope_and_collection(bucket, dc_scope, dc_col),
            cleanup=drop_scope(bucket, dc_scope),
        )
    )

    conv_scope = unique_name("acc_scope")
    cases.append(
        AccuracyCase(
            test_id="conversational_create_scope",
            prompt=(
                f"I need a brand-new scope called '{conv_scope}' set up in the "
                f"'{bucket}' bucket — can you make it?"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="create_scope",
                    parameters=Matcher.any_value(),
                ),
            ],
            cleanup=drop_scope(bucket, conv_scope),
        )
    )

    return cases


@pytest.fixture()
def collection_cases(test_bucket: str):
    return _build_cases(test_bucket)


COLLECTION_CASE_IDS = [
    "create_scope",
    "create_collection",
    "delete_scope",
    "delete_collection",
    "conversational_create_scope",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", COLLECTION_CASE_IDS)
async def test_collection_management_tool_accuracy(
    case_id: str,
    collection_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in collection_cases if c.test_id == case_id)
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
