"""Accuracy tests for the KV tools.

Cases:
  - get / insert / upsert / replace / delete (one each)
  - multi-step (get → upsert)
  - negative selection (a "read-only" prompt must not call delete)
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

# Local aliases keep the original call sites below unchanged while sourcing
# the seed/cleanup helpers from the shared sdk module.
_doc_id = doc_id
_seed_doc = seed_document
_delete_doc = delete_document


def _build_cases(bucket: str, scope: str, collection: str) -> list[AccuracyCase]:
    cases: list[AccuracyCase] = []

    get_id = _doc_id("acc_get")
    cases.append(
        AccuracyCase(
            test_id="get_document_by_id",
            prompt=(
                f"Fetch the document with id '{get_id}' from bucket "
                f"'{bucket}', scope '{scope}', collection '{collection}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_document_by_id",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "document_id": get_id,
                    },
                ),
            ],
            seed=_seed_doc(
                bucket, scope, collection, get_id, {"name": "Get", "purpose": "test"}
            ),
            cleanup=_delete_doc(bucket, scope, collection, get_id),
        )
    )

    insert_id = _doc_id("acc_insert")
    cases.append(
        AccuracyCase(
            test_id="insert_document_by_id",
            prompt=(
                f"Insert a new document with id '{insert_id}' into bucket "
                f"'{bucket}', scope '{scope}', collection '{collection}'. "
                'The document body should be {"name": "Inserted", "value": 1}. '
                "Use insert — fail if the document already exists; do not upsert."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="insert_document_by_id",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "document_id": insert_id,
                        "document_content": {"name": "Inserted", "value": 1},
                    },
                ),
            ],
            cleanup=_delete_doc(bucket, scope, collection, insert_id),
        )
    )

    upsert_id = _doc_id("acc_upsert")
    cases.append(
        AccuracyCase(
            test_id="upsert_document_by_id",
            prompt=(
                f"Upsert the document with id '{upsert_id}' into bucket "
                f"'{bucket}', scope '{scope}', collection '{collection}'. "
                'The document body should be {"name": "Upserted", "version": 1}. '
                "The operation must insert if missing or update if present."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="upsert_document_by_id",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "document_id": upsert_id,
                        "document_content": {"name": "Upserted", "version": 1},
                    },
                ),
            ],
            cleanup=_delete_doc(bucket, scope, collection, upsert_id),
        )
    )

    replace_id = _doc_id("acc_replace")
    cases.append(
        AccuracyCase(
            test_id="replace_document_by_id",
            prompt=(
                f"Replace the existing document with id '{replace_id}' in bucket "
                f"'{bucket}', scope '{scope}', collection '{collection}'. "
                'New body: {"name": "Replaced", "version": 2}. '
                "Replace only — fail if the document does not exist; do not upsert."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="replace_document_by_id",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "document_id": replace_id,
                        "document_content": {"name": "Replaced", "version": 2},
                    },
                ),
            ],
            seed=_seed_doc(
                bucket,
                scope,
                collection,
                replace_id,
                {"name": "Original", "version": 1},
            ),
            cleanup=_delete_doc(bucket, scope, collection, replace_id),
        )
    )

    delete_id = _doc_id("acc_delete")
    cases.append(
        AccuracyCase(
            test_id="delete_document_by_id",
            prompt=(
                f"Delete the document with id '{delete_id}' from bucket "
                f"'{bucket}', scope '{scope}', collection '{collection}'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="delete_document_by_id",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "document_id": delete_id,
                    },
                ),
            ],
            seed=_seed_doc(bucket, scope, collection, delete_id, {"name": "ToDelete"}),
        )
    )

    multi_id = _doc_id("acc_multi")
    cases.append(
        AccuracyCase(
            test_id="get_then_upsert_multistep",
            prompt=(
                f"Look up the document '{multi_id}' in bucket '{bucket}', scope "
                f"'{scope}', collection '{collection}'. Then upsert it back with "
                'an additional field {"status": "reviewed"} merged into its body.'
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_document_by_id",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "document_id": multi_id,
                    },
                ),
                ExpectedToolCall(
                    tool_name="upsert_document_by_id",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "document_id": multi_id,
                        "document_content": Matcher.any_value(),
                    },
                ),
            ],
            seed=_seed_doc(
                bucket, scope, collection, multi_id, {"name": "Doc", "status": "draft"}
            ),
            cleanup=_delete_doc(bucket, scope, collection, multi_id),
        )
    )

    read_only_id = _doc_id("acc_readonly")
    cases.append(
        AccuracyCase(
            test_id="read_only_prompt_uses_get_only",
            prompt=(
                f"Show me the contents of document '{read_only_id}' from bucket "
                f"'{bucket}', scope '{scope}', collection '{collection}'. "
                "Do not modify or delete it."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_document_by_id",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "document_id": read_only_id,
                    },
                ),
            ],
            seed=_seed_doc(
                bucket, scope, collection, read_only_id, {"name": "ReadOnly"}
            ),
            cleanup=_delete_doc(bucket, scope, collection, read_only_id),
        )
    )

    cases.append(
        AccuracyCase(
            test_id="conversational_lookup_document",
            prompt="I'm looking for a document — can you pull up the one with id 'doc_42'?",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_document_by_id",
                    parameters=Matcher.any_value(),
                ),
            ],
        )
    )

    return cases


@pytest.fixture()
def kv_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


KV_CASE_IDS = [
    "get_document_by_id",
    "insert_document_by_id",
    "upsert_document_by_id",
    "replace_document_by_id",
    "delete_document_by_id",
    "get_then_upsert_multistep",
    "read_only_prompt_uses_get_only",
    "conversational_lookup_document",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", KV_CASE_IDS)
async def test_kv_tool_accuracy(
    case_id: str,
    kv_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in kv_cases if c.test_id == case_id)
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
