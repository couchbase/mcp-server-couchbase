"""
Integration tests for the batch form of the KV write tools.

What matters against a live cluster is the account the batch gives back: every
requested id appears in exactly one of ``succeeded`` and ``errors``, one bad
document does not cost the rest, and an oversized batch is refused before
anything is written.
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

MAX_BULK_DOCS = 100


def _keyspace() -> dict[str, str]:
    return {
        "bucket_name": require_test_bucket(),
        "scope_name": get_test_scope(),
        "collection_name": get_test_collection(),
    }


def _ids(prefix: str, count: int) -> list[str]:
    run = uuid.uuid4().hex[:8]
    return [f"test_bulk_{prefix}_{run}_{i}" for i in range(count)]


async def _call(session, tool: str, **arguments) -> dict:
    response = await session.call_tool(tool, arguments={**_keyspace(), **arguments})
    assert not is_error_response(response), f"{tool} returned an error: {response}"
    return extract_payload(response)


async def _read(session, doc_id: str):
    return await session.call_tool(
        "get_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id},
    )


async def _cleanup(session, ids: list[str]) -> None:
    await session.call_tool(
        "delete_document_by_id",
        arguments={**_keyspace(), "document_ids": ids},
    )


@pytest.mark.asyncio
async def test_batch_upsert_writes_every_document() -> None:
    ids = _ids("upsert", 5)
    docs = {doc_id: {"n": i} for i, doc_id in enumerate(ids)}
    async with create_mcp_session() as session:
        try:
            result = await _call(session, "upsert_document_by_id", documents=docs)
            assert result["success"] is True
            assert sorted(result["succeeded"]) == sorted(ids)
            assert result["errors"] == {}
            for i, doc_id in enumerate(ids):
                assert extract_payload(await _read(session, doc_id)) == {"n": i}
        finally:
            await _cleanup(session, ids)


@pytest.mark.asyncio
async def test_batch_insert_reports_the_existing_key_and_writes_the_rest() -> None:
    ids = _ids("insert", 3)
    async with create_mcp_session() as session:
        try:
            await _call(
                session,
                "upsert_document_by_id",
                document_id=ids[0],
                document_content={"original": True},
            )
            result = await _call(
                session,
                "insert_document_by_id",
                documents={doc_id: {"inserted": True} for doc_id in ids},
            )
            assert set(result["succeeded"]) == set(ids[1:])
            assert set(result["errors"]) == {ids[0]}
            # Every requested id is accounted for exactly once.
            assert set(result["succeeded"]) | set(result["errors"]) == set(ids)
            assert not set(result["succeeded"]) & set(result["errors"])
            # The existing document was not overwritten by the failed insert.
            assert extract_payload(await _read(session, ids[0])) == {"original": True}
        finally:
            await _cleanup(session, ids)


@pytest.mark.asyncio
async def test_batch_replace_reports_the_missing_key_and_replaces_the_rest() -> None:
    ids = _ids("replace", 3)
    async with create_mcp_session() as session:
        try:
            for doc_id in ids[:2]:
                await _call(
                    session,
                    "upsert_document_by_id",
                    document_id=doc_id,
                    document_content={"version": 1},
                )
            result = await _call(
                session,
                "replace_document_by_id",
                documents={doc_id: {"version": 2} for doc_id in ids},
            )
            assert set(result["succeeded"]) == set(ids[:2])
            assert set(result["errors"]) == {ids[2]}
            for doc_id in ids[:2]:
                assert extract_payload(await _read(session, doc_id)) == {"version": 2}
        finally:
            await _cleanup(session, ids)


@pytest.mark.asyncio
async def test_batch_delete_reports_an_absent_key_rather_than_counting_it() -> None:
    ids = _ids("delete", 3)
    async with create_mcp_session() as session:
        try:
            docs = {doc_id: {"n": 1} for doc_id in ids[:2]}
            await _call(session, "upsert_document_by_id", documents=docs)
            result = await _call(session, "delete_document_by_id", document_ids=ids)
            assert set(result["succeeded"]) == set(ids[:2])
            assert set(result["errors"]) == {ids[2]}
            assert is_error_response(await _read(session, ids[0]))
        finally:
            await _cleanup(session, ids)


@pytest.mark.asyncio
async def test_oversized_batch_is_refused_and_nothing_is_written() -> None:
    ids = _ids("toobig", MAX_BULK_DOCS + 1)
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "upsert_document_by_id",
            arguments={**_keyspace(), "documents": {i: {"n": 1} for i in ids}},
        )
        payload = extract_payload(response)
        assert payload["success"] is False
        assert str(MAX_BULK_DOCS) in payload["error"]
        # Nothing was written: the first and last ids are both absent.
        assert is_error_response(await _read(session, ids[0]))
        assert is_error_response(await _read(session, ids[-1]))


@pytest.mark.asyncio
async def test_single_and_batch_arguments_together_are_refused() -> None:
    doc_id = _ids("both", 1)[0]
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "upsert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": {"n": 1},
                "documents": {doc_id: {"n": 2}},
            },
        )
        payload = extract_payload(response)
        assert payload["success"] is False
        assert is_error_response(await _read(session, doc_id))
