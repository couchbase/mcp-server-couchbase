"""
Integration tests for CAS-based optimistic concurrency.

The behaviour worth proving against a live server is not that the parameter is
accepted, but that it is *enforced*: a write carrying a superseded revision
must fail, and the concurrent update it would have destroyed must survive.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import (
    create_mcp_session,
    extract_payload,
    get_test_collection,
    get_test_scope,
    require_test_bucket,
)


def _keyspace() -> dict[str, str]:
    return {
        "bucket_name": require_test_bucket(),
        "scope_name": get_test_scope(),
        "collection_name": get_test_collection(),
    }


async def _seed(session, content: dict) -> str:
    doc_id = f"test_cas_{uuid.uuid4().hex[:8]}"
    await session.call_tool(
        "upsert_document_by_id",
        arguments={
            **_keyspace(),
            "document_id": doc_id,
            "document_content": content,
        },
    )
    return doc_id


async def _read_with_cas(session, doc_id: str) -> dict:
    response = await session.call_tool(
        "get_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id, "with_cas": True},
    )
    return extract_payload(response)


async def _read(session, doc_id: str) -> dict:
    response = await session.call_tool(
        "get_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id},
    )
    return extract_payload(response)


async def _delete(session, doc_id: str) -> None:
    await session.call_tool(
        "delete_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id},
    )


@pytest.mark.asyncio
async def test_default_read_returns_the_bare_document() -> None:
    """Existing callers must see no change."""
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"name": "unchanged", "value": 1})
        try:
            assert await _read(session, doc_id) == {"name": "unchanged", "value": 1}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_with_cas_returns_content_and_a_usable_cas() -> None:
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"name": "enveloped", "value": 2})
        try:
            payload = await _read_with_cas(session, doc_id)
            assert payload["content"] == {"name": "enveloped", "value": 2}
            assert payload["cas"].isdigit(), (
                f"expected a decimal CAS string, got {payload['cas']!r}"
            )
            assert int(payload["cas"]) > 0
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_cas_changes_when_the_document_is_rewritten() -> None:
    """A CAS that did not track the revision would be useless as a guard."""
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"version": 1})
        try:
            first = await _read_with_cas(session, doc_id)
            await session.call_tool(
                "upsert_document_by_id",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "document_content": {"version": 2},
                },
            )
            second = await _read_with_cas(session, doc_id)
            assert second["cas"] != first["cas"]
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_replace_with_current_cas_succeeds() -> None:
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"version": 1})
        try:
            current = await _read_with_cas(session, doc_id)
            response = await session.call_tool(
                "replace_document_by_id",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "document_content": {"version": 2},
                    "cas": current["cas"],
                },
            )
            payload = extract_payload(response)
            assert payload["success"] is True, (
                f"replace with the current CAS should succeed, got {payload}"
            )
            assert await _read(session, doc_id) == {"version": 2}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_replace_with_stale_cas_fails_and_preserves_the_other_write() -> None:
    """The whole point of the guard. Without it the concurrent update is lost
    silently, which is the failure this feature exists to prevent."""
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"version": 1})
        try:
            stale = await _read_with_cas(session, doc_id)

            # Someone else moves the document on.
            await session.call_tool(
                "upsert_document_by_id",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "document_content": {"version": 2},
                },
            )

            response = await session.call_tool(
                "replace_document_by_id",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "document_content": {"version": 3},
                    "cas": stale["cas"],
                },
            )
            assert extract_payload(response)["success"] is False, (
                "replace with a stale CAS must fail"
            )

            surviving = await _read(session, doc_id)
            assert surviving == {"version": 2}, (
                f"the concurrent write must survive, got {surviving}"
            )
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_delete_with_stale_cas_fails_and_document_survives() -> None:
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"version": 1})
        try:
            stale = await _read_with_cas(session, doc_id)
            await session.call_tool(
                "upsert_document_by_id",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "document_content": {"version": 2},
                },
            )

            response = await session.call_tool(
                "delete_document_by_id",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "cas": stale["cas"],
                },
            )
            assert extract_payload(response)["success"] is False

            assert await _read(session, doc_id) == {"version": 2}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_mutate_subdocument_with_stale_cas_fails() -> None:
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"counter": 1})
        try:
            stale = await _read_with_cas(session, doc_id)
            await session.call_tool(
                "upsert_document_by_id",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "document_content": {"counter": 2},
                },
            )

            response = await session.call_tool(
                "mutate_subdocument",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "upsert_specs": [{"path": "counter", "value": 3}],
                    "cas": stale["cas"],
                },
            )
            assert "error" in extract_payload(response)
            assert await _read(session, doc_id) == {"counter": 2}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_mutate_subdocument_with_current_cas_succeeds() -> None:
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"counter": 1})
        try:
            current = await _read_with_cas(session, doc_id)
            response = await session.call_tool(
                "mutate_subdocument",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "upsert_specs": [{"path": "counter", "value": 3}],
                    "cas": current["cas"],
                },
            )
            payload = extract_payload(response)
            assert payload["upsert"]["counter"] == {"success": True}, payload
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_malformed_cas_is_rejected_and_nothing_is_written() -> None:
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"version": 1})
        try:
            response = await session.call_tool(
                "replace_document_by_id",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "document_content": {"version": 99},
                    "cas": "not-a-number",
                },
            )
            payload = extract_payload(response)
            assert payload["success"] is False
            assert "cas" in payload["error"]
            assert await _read(session, doc_id) == {"version": 1}
        finally:
            await _delete(session, doc_id)
