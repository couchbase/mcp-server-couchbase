"""
Integration tests for store_semantics on mutate_subdocument.

The behaviour only a live server can show is the difference the parameter
makes: the same call that fails against a missing document under the default
semantics must succeed, and create the document, under "UPSERT".
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


async def _delete(session, doc_id: str) -> None:
    await session.call_tool(
        "delete_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id},
    )


async def _read(session, doc_id: str) -> dict:
    response = await session.call_tool(
        "get_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id},
    )
    return extract_payload(response)


@pytest.mark.asyncio
async def test_default_semantics_still_fail_on_a_missing_document() -> None:
    """The previous behaviour must be unchanged when the parameter is omitted."""
    doc_id = f"test_sem_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "upsert_specs": [{"path": "created", "value": True}],
            },
        )
        assert "error" in extract_payload(response)


@pytest.mark.asyncio
async def test_upsert_semantics_creates_the_missing_document() -> None:
    doc_id = f"test_sem_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "upsert_specs": [{"path": "created", "value": True}],
                "store_semantics": "UPSERT",
            },
        )
        payload = extract_payload(response)
        try:
            assert payload["upsert"]["created"] == {"success": True}, payload
            assert await _read(session, doc_id) == {"created": True}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_upsert_semantics_also_works_on_an_existing_document() -> None:
    doc_id = f"test_sem_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": {"existing": True},
            },
        )
        try:
            response = await session.call_tool(
                "mutate_subdocument",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "upsert_specs": [{"path": "added", "value": 1}],
                    "store_semantics": "UPSERT",
                },
            )
            assert extract_payload(response)["upsert"]["added"] == {"success": True}
            assert await _read(session, doc_id) == {"existing": True, "added": 1}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_insert_semantics_fails_when_the_document_exists() -> None:
    doc_id = f"test_sem_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": {"existing": True},
            },
        )
        try:
            response = await session.call_tool(
                "mutate_subdocument",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "upsert_specs": [{"path": "added", "value": 1}],
                    "store_semantics": "INSERT",
                },
            )
            assert "error" in extract_payload(response)
            assert await _read(session, doc_id) == {"existing": True}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_insert_semantics_creates_a_missing_document() -> None:
    doc_id = f"test_sem_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "upsert_specs": [{"path": "created", "value": True}],
                "store_semantics": "INSERT",
            },
        )
        try:
            assert extract_payload(response)["upsert"]["created"] == {"success": True}
            assert await _read(session, doc_id) == {"created": True}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_unknown_semantics_is_rejected_and_nothing_is_created() -> None:
    doc_id = f"test_sem_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "upsert_specs": [{"path": "created", "value": True}],
                "store_semantics": "CREATE_IF_MISSING",
            },
        )
        payload = extract_payload(response)
        assert "CREATE_IF_MISSING" in payload["error"]

        check = await session.call_tool(
            "get_document_by_id",
            arguments={**_keyspace(), "document_id": doc_id},
        )
        assert check.isError, "a rejected mutation must not have created the document"
