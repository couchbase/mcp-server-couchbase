"""
Integration tests for run_kv_transaction.

The property worth proving against a live cluster is atomicity: a transaction
either lands every operation or none of them, and a rollback leaves no trace of
the operations that ran before the failing one.
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

MAX_TRANSACTION_OPS = 50


def _target(doc_id: str) -> dict[str, str]:
    return {
        "bucket_name": require_test_bucket(),
        "scope_name": get_test_scope(),
        "collection_name": get_test_collection(),
        "document_id": doc_id,
    }


def _ids(prefix: str, count: int) -> list[str]:
    run = uuid.uuid4().hex[:8]
    return [f"test_txn_{prefix}_{run}_{i}" for i in range(count)]


async def _transaction(session, operations: list[dict], **extra) -> dict:
    response = await session.call_tool(
        "run_kv_transaction", arguments={"operations": operations, **extra}
    )
    return extract_payload(response)


async def _read(session, doc_id: str):
    return await session.call_tool(
        "get_document_by_id",
        arguments={**_target(doc_id)},
    )


async def _cleanup(session, ids: list[str]) -> None:
    for doc_id in ids:
        await session.call_tool("delete_document_by_id", arguments={**_target(doc_id)})


@pytest.mark.asyncio
async def test_transaction_commits_every_operation() -> None:
    ids = _ids("commit", 2)
    async with create_mcp_session() as session:
        try:
            result = await _transaction(
                session,
                [
                    {"op": "insert", **_target(ids[0]), "content": {"n": 0}},
                    {"op": "insert", **_target(ids[1]), "content": {"n": 1}},
                ],
            )
            assert result["success"] is True
            assert result["committed"] is True
            for i, doc_id in enumerate(ids):
                assert extract_payload(await _read(session, doc_id)) == {"n": i}
        finally:
            await _cleanup(session, ids)


@pytest.mark.asyncio
async def test_a_failing_operation_rolls_back_the_ones_before_it() -> None:
    """The insert would succeed on its own. It must not survive the failed replace."""
    inserted, missing = _ids("rollback", 2)
    async with create_mcp_session() as session:
        try:
            result = await _transaction(
                session,
                [
                    {"op": "insert", **_target(inserted), "content": {"n": 1}},
                    {"op": "replace", **_target(missing), "content": {"n": 2}},
                ],
            )
            assert result["success"] is False
            assert result["committed"] is False
            assert is_error_response(await _read(session, inserted)), (
                "The insert ran inside a transaction that rolled back, so the "
                "document must not exist"
            )
        finally:
            await _cleanup(session, [inserted, missing])


@pytest.mark.asyncio
async def test_mixed_operations_apply_together() -> None:
    kept, replaced, removed = _ids("mixed", 3)
    async with create_mcp_session() as session:
        try:
            for doc_id in (replaced, removed):
                await session.call_tool(
                    "upsert_document_by_id",
                    arguments={**_target(doc_id), "document_content": {"v": 1}},
                )
            result = await _transaction(
                session,
                [
                    {"op": "upsert", **_target(kept), "content": {"v": "new"}},
                    {"op": "replace", **_target(replaced), "content": {"v": 2}},
                    {"op": "remove", **_target(removed)},
                ],
            )
            assert result["committed"] is True
            assert extract_payload(await _read(session, kept)) == {"v": "new"}
            assert extract_payload(await _read(session, replaced)) == {"v": 2}
            assert is_error_response(await _read(session, removed))
        finally:
            await _cleanup(session, [kept, replaced, removed])


@pytest.mark.asyncio
async def test_a_malformed_operation_is_refused_before_anything_runs() -> None:
    doc_id = _ids("malformed", 1)[0]
    async with create_mcp_session() as session:
        try:
            result = await _transaction(
                session,
                [
                    {"op": "insert", **_target(doc_id), "content": {"n": 1}},
                    {"op": "not_an_operation", **_target(doc_id)},
                ],
            )
            assert result["success"] is False
            assert result["committed"] is False
            assert is_error_response(await _read(session, doc_id))
        finally:
            await _cleanup(session, [doc_id])


@pytest.mark.asyncio
async def test_more_operations_than_the_limit_are_refused() -> None:
    ids = _ids("toomany", MAX_TRANSACTION_OPS + 1)
    async with create_mcp_session() as session:
        result = await _transaction(
            session,
            [{"op": "insert", **_target(i), "content": {"n": 1}} for i in ids],
        )
        assert result["success"] is False
        assert result["committed"] is False
        assert is_error_response(await _read(session, ids[0]))
        assert is_error_response(await _read(session, ids[-1]))
