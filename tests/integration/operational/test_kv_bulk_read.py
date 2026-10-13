"""
Integration tests for the batch form of get_document_by_id.

What matters against a live cluster is the account the batch gives back: every
requested id appears in exactly one of ``documents``, ``errors``, ``oversize`` and ``not_returned``, one missing
document does not cost the rest, a batch over the count limit is refused before
the cluster is touched, and the combined size of what comes back is bounded.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import (
    create_mcp_session,
    extract_payload,
    get_test_collection,
    get_test_scope,
    is_error_response,
    require_test_bucket,
)

MAX_BULK_READ_DOCS = 50
DEFAULT_BULK_READ_BYTES = 64 * 1024


def _keyspace() -> dict[str, str]:
    return {
        "bucket_name": require_test_bucket(),
        "scope_name": get_test_scope(),
        "collection_name": get_test_collection(),
    }


def _ids(prefix: str, count: int) -> list[str]:
    run = uuid.uuid4().hex[:8]
    return [f"test_bulkread_{prefix}_{run}_{i}" for i in range(count)]


async def _write(session, doc_id: str, content: dict) -> None:
    response = await session.call_tool(
        "upsert_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id, "document_content": content},
    )
    assert extract_payload(response) == {"success": True}


async def _read_batch(session, ids: list[str]):
    return await session.call_tool(
        "get_document_by_id",
        arguments={**_keyspace(), "document_ids": ids},
    )


async def _cleanup(session, ids: list[str]) -> None:
    for doc_id in ids:
        await session.call_tool(
            "delete_document_by_id",
            arguments={**_keyspace(), "document_id": doc_id},
        )


@pytest.mark.asyncio
async def test_batch_read_returns_every_document() -> None:
    ids = _ids("all", 5)
    async with create_mcp_session() as session:
        try:
            for i, doc_id in enumerate(ids):
                await _write(session, doc_id, {"n": i})
            result = extract_payload(await _read_batch(session, ids))
            assert result["success"] is True
            assert result["errors"] == {}
            assert result["documents"] == {
                doc_id: {"n": i} for i, doc_id in enumerate(ids)
            }
        finally:
            await _cleanup(session, ids)


@pytest.mark.asyncio
async def test_a_missing_document_is_reported_and_the_rest_are_returned() -> None:
    ids = _ids("missing", 3)
    async with create_mcp_session() as session:
        try:
            for i, doc_id in enumerate(ids[:2]):
                await _write(session, doc_id, {"n": i})
            result = extract_payload(await _read_batch(session, ids))
            assert result["success"] is True
            assert set(result["documents"]) == set(ids[:2])
            assert set(result["errors"]) == {ids[2]}
            # Every requested id is in exactly one of the two.
            assert (
                set(result["documents"])
                | set(result["errors"])
                | set(result["oversize"])
                | set(result["not_returned"])
            ) == set(ids)
        finally:
            await _cleanup(session, ids)


@pytest.mark.asyncio
async def test_a_repeated_id_is_read_once() -> None:
    doc_id = _ids("repeat", 1)[0]
    async with create_mcp_session() as session:
        try:
            await _write(session, doc_id, {"n": 1})
            result = extract_payload(await _read_batch(session, [doc_id, doc_id]))
            assert list(result["documents"]) == [doc_id]
            assert result["errors"] == {}
        finally:
            await _cleanup(session, [doc_id])


@pytest.mark.asyncio
async def test_oversized_batch_is_refused() -> None:
    ids = _ids("toomany", MAX_BULK_READ_DOCS + 1)
    async with create_mcp_session() as session:
        payload = extract_payload(await _read_batch(session, ids))
        assert payload["success"] is False
        assert str(MAX_BULK_READ_DOCS) in payload["error"]


@pytest.mark.asyncio
async def test_single_and_batch_arguments_together_are_refused() -> None:
    doc_id = _ids("both", 1)[0]
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_document_by_id",
            arguments={**_keyspace(), "document_id": doc_id, "document_ids": [doc_id]},
        )
        assert extract_payload(response)["success"] is False


@pytest.mark.asyncio
async def test_neither_argument_is_refused() -> None:
    async with create_mcp_session() as session:
        response = await session.call_tool("get_document_by_id", arguments=_keyspace())
        assert extract_payload(response)["success"] is False


@pytest.mark.asyncio
async def test_the_combined_size_of_the_result_is_bounded() -> None:
    """Two documents that each fit but together do not: one is returned, the other
    is listed in not_returned, and a second call for it succeeds."""
    ids = _ids("size", 2)
    half = DEFAULT_BULK_READ_BYTES // 2 + 1000
    async with create_mcp_session() as session:
        try:
            for doc_id in ids:
                await _write(session, doc_id, {"blob": "x" * half})
            result = extract_payload(await _read_batch(session, ids))
            assert result["success"] is True
            assert list(result["documents"]) == [ids[0]]
            assert result["not_returned"] == [ids[1]]
            assert result["truncated"] is True
            assert result["bytes_returned"] <= result["limit_bytes"]
            followup = extract_payload(
                await _read_batch(session, result["not_returned"])
            )
            assert list(followup["documents"]) == [ids[1]]
            assert followup["truncated"] is False
        finally:
            await _cleanup(session, ids)


@pytest.mark.asyncio
async def test_a_document_over_the_whole_limit_is_oversize_and_not_cut() -> None:
    doc_id = _ids("oversize", 1)[0]
    async with create_mcp_session() as session:
        try:
            await _write(
                session, doc_id, {"blob": "x" * (DEFAULT_BULK_READ_BYTES + 100)}
            )
            result = extract_payload(await _read_batch(session, [doc_id]))
            assert result["documents"] == {}
            assert result["oversize"][doc_id] > DEFAULT_BULK_READ_BYTES
            assert result["truncated"] is False
            # Read on its own, as the guidance says.
            response = await session.call_tool(
                "get_document_by_id",
                arguments={**_keyspace(), "document_id": doc_id},
            )
            assert (
                len(extract_payload(response)["blob"]) == DEFAULT_BULK_READ_BYTES + 100
            )
        finally:
            await _cleanup(session, [doc_id])


@pytest.mark.asyncio
async def test_single_document_read_is_unchanged() -> None:
    doc_id = _ids("single", 1)[0]
    async with create_mcp_session() as session:
        try:
            await _write(session, doc_id, {"n": 7})
            response = await session.call_tool(
                "get_document_by_id",
                arguments={**_keyspace(), "document_id": doc_id},
            )
            assert extract_payload(response) == {"n": 7}
        finally:
            await _cleanup(session, [doc_id])
            absent = await session.call_tool(
                "get_document_by_id",
                arguments={**_keyspace(), "document_id": doc_id},
            )
            assert is_error_response(absent)
