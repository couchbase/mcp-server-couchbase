"""Accuracy tests for the vector search tools.

Covers:
  - run_vector_query: no index_name (GSI selects the index automatically),
    unlike every other search/query tool that takes one.
  - run_vector_search: index_name required, plus the hybrid
    (scalar_query present) vs. vector-only distinction.
  - Disambiguation: a plain full-text relevance prompt should still select
    run_search, not one of these two, and a "find similar/semantically
    related" prompt should select run_vector_query over run_search.

run_vector_query_no_index_name and conversational_plain_fts_not_vector_search
seed a real GSI vector index / Search index respectively: both
run_vector_query (distance_metric has no default -- its docstring says to
read `similarity` off a matching index via list_indexes) and run_search
(its docstring says to confirm the index first via list_search_indexes /
get_search_index_definition) instruct the model to verify the index exists
before querying. Without a real index to find, the model correctly refuses
to guess and never calls the expected tool at all -- matching every other
index/FTS-backed accuracy test file (test_fts.py, test_index.py), which
seed real state for the same reason. The other two cases don't need this:
run_vector_search's docstring explicitly says it's safe to call without
confirming first ("this tool does not pre-validate index_name"), so
index_name there stays a plausible, not necessarily-existing, name.
"""

from __future__ import annotations

import json
import random
import uuid

import pytest

from accuracy.sdk import (
    AccuracyCase,
    DiskResultStorage,
    Matcher,
    OpenAIAgent,
    drop_fts_index,
    run_accuracy_case,
    seed_fts_index,
)
from accuracy.sdk.client import AccuracyTestingClient
from accuracy.sdk.runner import SetupHook
from accuracy.sdk.types import ExpectedToolCall

_VECTOR_DIMENSION = 1536
# Empirically the minimum that reliably clears Couchbase's IVF centroid
# training floor (32 centroids by default) for an immediate (non-deferred)
# vector index build -- fewer qualifying vectors than centroids fails the
# build outright with ErrTraining.
_PROBE_DOC_COUNT = 50


def _seed_vector_index(
    bucket: str, scope: str, collection: str, index_name: str, doc_prefix: str
) -> SetupHook:
    """Return a hook that seeds real embedding-bearing documents and then
    creates a built Composite Vector Index on them.

    A Composite/Hyperscale Vector Index build trains IVF centroids from the
    collection's actual vectors -- creating one with deferred=False (needed
    so run_vector_query's list_indexes-first guidance finds it `online`,
    not just `deferred`) against a collection with no matching vectors
    fails outright with ErrTraining ("fewer qualifying vectors than
    centroids"). _PROBE_DOC_COUNT throwaway documents with a real
    `embedding` float array give it enough vectors to train on.
    """

    async def _hook(client: AccuracyTestingClient) -> None:
        rng = random.Random(1234567)  # noqa: S311 -- deterministic test fixture data, not security-sensitive
        for i in range(_PROBE_DOC_COUNT):
            await client.call_tool_silent(
                "upsert_document_by_id",
                {
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "document_id": f"{doc_prefix}_{i}",
                    "document_content": {
                        "type": doc_prefix,
                        "embedding": [
                            rng.uniform(-1, 1) for _ in range(_VECTOR_DIMENSION)
                        ],
                    },
                },
            )
        await client.call_tool_silent(
            "create_query_index",
            {
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "index_name": index_name,
                "index_type": "composite_vector",
                "vector_field": "embedding",
                "dimension": _VECTOR_DIMENSION,
                "similarity": "COSINE",
                "deferred": False,
            },
        )

    return _hook


def _drop_vector_index(
    bucket: str, scope: str, collection: str, index_name: str, doc_prefix: str
) -> SetupHook:
    """Return a hook that drops the vector index and its probe documents
    (silently, best-effort)."""

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
        for i in range(_PROBE_DOC_COUNT):
            await client.call_tool_silent(
                "delete_document_by_id",
                {
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                    "document_id": f"{doc_prefix}_{i}",
                },
            )

    return _hook


def _build_cases(bucket: str, scope: str, collection: str) -> list[AccuracyCase]:
    # Unique per test run (matching test_fts.py/test_index.py's convention) --
    # fixed names could collide with real data or a concurrent accuracy run
    # sharing the same cluster, since seeding/cleanup upsert/drop/delete these
    # unconditionally.
    index_name = f"product_vector_index_{uuid.uuid4().hex[:8]}"
    gsi_vector_index_name = f"acc_vec_idx_embedding_{uuid.uuid4().hex[:8]}"
    gsi_probe_doc_prefix = f"acc_vec_probe_{uuid.uuid4().hex[:8]}"
    seed_gsi_vector_index = _seed_vector_index(
        bucket, scope, collection, gsi_vector_index_name, gsi_probe_doc_prefix
    )
    drop_gsi_vector_index = _drop_vector_index(
        bucket, scope, collection, gsi_vector_index_name, gsi_probe_doc_prefix
    )
    seed_fts_vector_index = seed_fts_index(bucket, scope, collection, index_name)
    drop_fts_vector_index = drop_fts_index(bucket, scope, index_name)

    return [
        AccuracyCase(
            test_id="run_vector_query_no_index_name",
            prompt=(
                f"In the '{collection}' collection (scope '{scope}', bucket "
                f"'{bucket}'), find documents whose 'embedding' field is "
                f"semantically closest to the text 'a warm winter jacket'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_vector_query",
                    parameters={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                        "vector_field": "embedding",
                        "query_text": Matcher.any_value(),
                    },
                ),
            ],
            seed=seed_gsi_vector_index,
            cleanup=drop_gsi_vector_index,
        ),
        AccuracyCase(
            test_id="run_vector_search_named_index",
            prompt=(
                f"Using the Search index '{index_name}', run a vector "
                f"similarity search on the 'embedding' field for text "
                f"semantically similar to 'a warm winter jacket'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_vector_search",
                    parameters={
                        "index_name": index_name,
                        "vector_field": "embedding",
                        "vector_query_text": Matcher.any_value(),
                    },
                ),
            ],
        ),
        AccuracyCase(
            test_id="run_vector_search_hybrid",
            prompt=(
                f"Using the Search index '{index_name}', find products whose "
                f"'embedding' field is semantically similar to 'a warm winter "
                f"jacket', but only among products whose description also "
                f"contains the word 'jacket'."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_vector_search",
                    parameters={
                        "index_name": index_name,
                        "vector_field": "embedding",
                        "vector_query_text": Matcher.any_value(),
                        "scalar_query": Matcher.any_value(),
                    },
                ),
            ],
        ),
        AccuracyCase(
            test_id="conversational_plain_fts_not_vector_search",
            prompt=(
                f"In the '{collection}' collection (scope '{scope}', bucket "
                f"'{bucket}'), search the '{index_name}' Search index for "
                f"documents whose text fuzzily matches the word 'jacket', "
                f"ranked by relevance score."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="run_search",
                    parameters=Matcher.any_value(),
                ),
            ],
            seed=seed_fts_vector_index,
            cleanup=drop_fts_vector_index,
        ),
    ]


@pytest.fixture()
def vector_search_cases(test_bucket: str, test_scope: str, test_collection: str):
    return _build_cases(test_bucket, test_scope, test_collection)


VECTOR_SEARCH_CASE_IDS = [
    "run_vector_query_no_index_name",
    "run_vector_search_named_index",
    "run_vector_search_hybrid",
    "conversational_plain_fts_not_vector_search",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", VECTOR_SEARCH_CASE_IDS)
async def test_vector_search_tool_accuracy(
    case_id: str,
    vector_search_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in vector_search_cases if c.test_id == case_id)
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
