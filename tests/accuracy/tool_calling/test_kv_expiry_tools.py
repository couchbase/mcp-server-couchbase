"""Accuracy tests for the existence and expiry KV tools.

Cases:
  - document_exists_by_id chosen over get_document_by_id for a presence check
  - document_exists_by_id chosen over lookup_subdocument, which checks whether
    a *path* exists rather than whether the document does
  - touch_document_by_id chosen for a TTL change that must not rewrite content
  - get_and_touch_document_by_id chosen when the read is itself the reason to
    extend the life of the document

The last three are the ones worth having. Each names a job the existing tools
can almost do, which is exactly where a model picks the near-miss: a full
document write instead of a touch, or a get followed by a separate touch
instead of the single round trip.
"""

from __future__ import annotations

import json

import pytest

from accuracy.sdk import (
    AccuracyCase,
    DiskResultStorage,
    Matcher,
    OpenAIAgent,
    delete_document,
    doc_id,
    run_accuracy_case,
    seed_document,
)
from accuracy.sdk.types import ExpectedToolCall


def _build_cases(bucket: str, scope: str, collection: str) -> list[AccuracyCase]:
    cases: list[AccuracyCase] = []
    keyspace = {
        "bucket_name": bucket,
        "scope_name": scope,
        "collection_name": collection,
    }

    exists_id = doc_id("acc_exists")
    cases.append(
        AccuracyCase(
            test_id="exists_chosen_over_get",
            prompt=(
                f"Is there a document with id '{exists_id}' in bucket '{bucket}', "
                f"scope '{scope}', collection '{collection}'? I only need to know "
                "whether it is there, not what is in it."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="document_exists_by_id",
                    parameters={**keyspace, "document_id": exists_id},
                ),
            ],
            seed=seed_document(
                bucket, scope, collection, exists_id, {"name": "Exists"}
            ),
            cleanup=delete_document(bucket, scope, collection, exists_id),
        )
    )

    missing_id = doc_id("acc_exists_missing")
    cases.append(
        AccuracyCase(
            test_id="exists_used_for_a_document_that_is_not_there",
            prompt=(
                f"Before I insert it, tell me whether document '{missing_id}' "
                f"already exists in bucket '{bucket}', scope '{scope}', "
                f"collection '{collection}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="document_exists_by_id",
                    parameters={**keyspace, "document_id": missing_id},
                ),
            ],
        )
    )

    path_id = doc_id("acc_exists_vs_lookup")
    cases.append(
        AccuracyCase(
            test_id="document_existence_is_not_a_subdocument_lookup",
            prompt=(
                f"Does the document '{path_id}' exist at all in bucket '{bucket}', "
                f"scope '{scope}', collection '{collection}'? I am asking about "
                "the document itself, not about any field inside it."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="document_exists_by_id",
                    parameters={**keyspace, "document_id": path_id},
                ),
            ],
            seed=seed_document(
                bucket, scope, collection, path_id, {"name": "Lookup", "tags": ["a"]}
            ),
            cleanup=delete_document(bucket, scope, collection, path_id),
        )
    )

    touch_id = doc_id("acc_touch")
    cases.append(
        AccuracyCase(
            test_id="touch_chosen_over_rewriting_the_document",
            prompt=(
                f"Make document '{touch_id}' in bucket '{bucket}', scope '{scope}', "
                f"collection '{collection}' expire in one hour. Do not change its "
                "contents."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="touch_document_by_id",
                    parameters={
                        **keyspace,
                        "document_id": touch_id,
                        "expiry_seconds": Matcher.any_value(),
                    },
                ),
            ],
            seed=seed_document(
                bucket, scope, collection, touch_id, {"name": "Touch", "state": "warm"}
            ),
            cleanup=delete_document(bucket, scope, collection, touch_id),
        )
    )

    clear_id = doc_id("acc_touch_clear")
    cases.append(
        AccuracyCase(
            test_id="touch_with_zero_chosen_to_clear_a_ttl",
            prompt=(
                f"Document '{clear_id}' in bucket '{bucket}', scope '{scope}', "
                f"collection '{collection}' is set to expire. Cancel that so it is "
                "kept indefinitely, without rewriting the document."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="touch_document_by_id",
                    parameters={
                        **keyspace,
                        "document_id": clear_id,
                        "expiry_seconds": 0,
                    },
                ),
            ],
            seed=seed_document(bucket, scope, collection, clear_id, {"name": "Clear"}),
            cleanup=delete_document(bucket, scope, collection, clear_id),
        )
    )

    gat_id = doc_id("acc_gat")
    cases.append(
        AccuracyCase(
            test_id="get_and_touch_chosen_for_read_plus_extend",
            prompt=(
                f"Read document '{gat_id}' from bucket '{bucket}', scope '{scope}', "
                f"collection '{collection}' and, in the same operation, push its "
                "expiry out to 30 minutes from now because I am still using it."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_and_touch_document_by_id",
                    parameters={
                        **keyspace,
                        "document_id": gat_id,
                        "expiry_seconds": Matcher.any_value(),
                    },
                ),
            ],
            seed=seed_document(
                bucket, scope, collection, gat_id, {"name": "Lease", "holder": "agent"}
            ),
            cleanup=delete_document(bucket, scope, collection, gat_id),
        )
    )

    return cases


@pytest.fixture()
def expiry_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


EXPIRY_CASE_IDS = [
    "exists_chosen_over_get",
    "exists_used_for_a_document_that_is_not_there",
    "document_existence_is_not_a_subdocument_lookup",
    "touch_chosen_over_rewriting_the_document",
    "touch_with_zero_chosen_to_clear_a_ttl",
    "get_and_touch_chosen_for_read_plus_extend",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", EXPIRY_CASE_IDS)
async def test_expiry_tool_accuracy(
    case_id: str,
    expiry_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in expiry_cases if c.test_id == case_id)
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
