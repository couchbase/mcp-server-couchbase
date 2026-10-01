"""
Integration tests for document_exists_by_id, touch and get_and_touch.

What only a live server can show: that a TTL set by touch really does make the
document stop being readable, and that clearing it really does keep the
document alive. Couchbase applies expiry lazily, so these poll rather than
sleeping for a fixed time and checking once.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from conftest import (
    create_mcp_session,
    extract_payload,
    get_test_collection,
    get_test_scope,
    require_test_bucket,
)

EXPIRY_POLL_TIMEOUT = 45


def _keyspace() -> dict[str, str]:
    return {
        "bucket_name": require_test_bucket(),
        "scope_name": get_test_scope(),
        "collection_name": get_test_collection(),
    }


async def _seed(session, doc_id: str, content: dict) -> None:
    await session.call_tool(
        "upsert_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id, "document_content": content},
    )


async def _delete(session, doc_id: str) -> None:
    await session.call_tool(
        "delete_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id},
    )


async def _exists(session, doc_id: str) -> dict:
    return extract_payload(
        await session.call_tool(
            "document_exists_by_id",
            arguments={**_keyspace(), "document_id": doc_id},
        )
    )


async def _wait_until_gone(session, doc_id: str) -> bool:
    deadline = time.monotonic() + EXPIRY_POLL_TIMEOUT
    while time.monotonic() < deadline:
        if (await _exists(session, doc_id)).get("exists") is False:
            return True
        await asyncio.sleep(1)
    return False


@pytest.mark.asyncio
async def test_exists_reports_true_for_a_present_document() -> None:
    doc_id = f"test_exists_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"n": 1})
            assert await _exists(session, doc_id) == {"exists": True}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_exists_reports_false_for_a_missing_document() -> None:
    """Absence is a normal answer, not an error, unlike get_document_by_id."""
    doc_id = f"test_exists_missing_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        payload = await _exists(session, doc_id)
        assert payload == {"exists": False}
        assert "error" not in payload


@pytest.mark.asyncio
async def test_exists_tracks_a_delete() -> None:
    doc_id = f"test_exists_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        await _seed(session, doc_id, {"n": 1})
        assert (await _exists(session, doc_id))["exists"] is True
        await _delete(session, doc_id)
        assert (await _exists(session, doc_id))["exists"] is False


@pytest.mark.asyncio
async def test_touch_sets_a_ttl_that_actually_expires() -> None:
    doc_id = f"test_touch_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"n": 1})
            response = await session.call_tool(
                "touch_document_by_id",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "expiry_seconds": 3,
                },
            )
            assert extract_payload(response)["success"] is True
            assert (await _exists(session, doc_id))["exists"] is True

            assert await _wait_until_gone(session, doc_id), (
                f"Document with a 3s TTL was still present after {EXPIRY_POLL_TIMEOUT}s"
            )
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_touch_with_zero_clears_an_existing_ttl() -> None:
    """The document must survive well past the TTL it previously carried."""
    doc_id = f"test_touch_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"n": 1})
            await session.call_tool(
                "touch_document_by_id",
                arguments={**_keyspace(), "document_id": doc_id, "expiry_seconds": 3},
            )
            await session.call_tool(
                "touch_document_by_id",
                arguments={**_keyspace(), "document_id": doc_id, "expiry_seconds": 0},
            )

            await asyncio.sleep(8)
            assert (await _exists(session, doc_id))["exists"] is True
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_touch_on_a_missing_document_reports_an_error() -> None:
    doc_id = f"test_touch_missing_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "touch_document_by_id",
            arguments={**_keyspace(), "document_id": doc_id, "expiry_seconds": 60},
        )
        payload = extract_payload(response)
        assert payload["success"] is False


@pytest.mark.asyncio
async def test_negative_expiry_is_rejected_and_nothing_changes() -> None:
    doc_id = f"test_touch_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"n": 1})
            response = await session.call_tool(
                "touch_document_by_id",
                arguments={**_keyspace(), "document_id": doc_id, "expiry_seconds": -1},
            )
            payload = extract_payload(response)
            assert payload["success"] is False
            assert "expiry_seconds" in payload["error"]
            assert (await _exists(session, doc_id))["exists"] is True
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_get_and_touch_returns_the_document_and_sets_the_ttl() -> None:
    doc_id = f"test_gat_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"field": "value"})
            response = await session.call_tool(
                "get_and_touch_document_by_id",
                arguments={**_keyspace(), "document_id": doc_id, "expiry_seconds": 3},
            )
            payload = extract_payload(response)
            assert payload == {"field": "value"}

            assert await _wait_until_gone(session, doc_id), (
                f"Document get_and_touched with a 3s TTL was still present after "
                f"{EXPIRY_POLL_TIMEOUT}s"
            )
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_get_and_touch_with_zero_returns_the_document_and_keeps_it() -> None:
    doc_id = f"test_gat_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"field": "value"})
            response = await session.call_tool(
                "get_and_touch_document_by_id",
                arguments={**_keyspace(), "document_id": doc_id, "expiry_seconds": 0},
            )
            assert extract_payload(response) == {"field": "value"}
            await asyncio.sleep(5)
            assert (await _exists(session, doc_id))["exists"] is True
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_get_and_touch_on_a_missing_document_reports_an_error() -> None:
    doc_id = f"test_gat_missing_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_and_touch_document_by_id",
            arguments={**_keyspace(), "document_id": doc_id, "expiry_seconds": 60},
        )
        payload = extract_payload(response)
        assert payload["success"] is False


@pytest.mark.asyncio
async def test_read_only_mode_keeps_exists_and_drops_the_touch_tools() -> None:
    """exists is a read, touch and get_and_touch are writes.

    get_and_touch reads a document but changes its expiry, so it belongs with
    the write tools and must disappear from a read-only session.
    """
    async with create_mcp_session(
        extra_env={"CB_MCP_READ_ONLY_MODE": "true"}
    ) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}

    assert "document_exists_by_id" in names
    assert "touch_document_by_id" not in names
    assert "get_and_touch_document_by_id" not in names
