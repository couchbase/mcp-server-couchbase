"""
Integration tests for get_documents_by_ids (bulk document read).

Covers what a live server shows that mocks cannot: that a batch really is one
round trip against real documents, that partial results behave as documented
when some IDs are absent, and that the tool survives read-only filtering.
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

from cb_mcp.tools.kv import MAX_BULK_GET_IDS

READ_ONLY_ENV = {"CB_MCP_READ_ONLY_MODE": "true"}


def _keyspace() -> dict[str, str]:
    return {
        "bucket_name": require_test_bucket(),
        "scope_name": get_test_scope(),
        "collection_name": get_test_collection(),
    }


async def _seed(session, count: int) -> dict[str, dict]:
    """Create `count` documents and return {id: content}."""
    seeded = {}
    for index in range(count):
        doc_id = f"test_bulk_{uuid.uuid4().hex[:8]}"
        content = {"index": index, "name": f"doc {index}"}
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": content,
            },
        )
        seeded[doc_id] = content
    return seeded


async def _cleanup(session, doc_ids) -> None:
    for doc_id in doc_ids:
        await session.call_tool(
            "delete_document_by_id",
            arguments={**_keyspace(), "document_id": doc_id},
        )


@pytest.mark.asyncio
async def test_reads_a_batch_of_documents() -> None:
    async with create_mcp_session() as session:
        seeded = await _seed(session, 3)
        try:
            response = await session.call_tool(
                "get_documents_by_ids",
                arguments={**_keyspace(), "document_ids": list(seeded)},
            )
            payload = extract_payload(response)

            assert payload["errors"] == {}, payload["errors"]
            assert payload["documents"] == seeded
        finally:
            await _cleanup(session, seeded)


@pytest.mark.asyncio
async def test_missing_ids_are_reported_without_losing_the_batch() -> None:
    """A partial result is the useful behaviour: present documents still come
    back, absent ones are named."""
    async with create_mcp_session() as session:
        seeded = await _seed(session, 2)
        missing_id = f"test_bulk_absent_{uuid.uuid4().hex[:8]}"
        try:
            response = await session.call_tool(
                "get_documents_by_ids",
                arguments={**_keyspace(), "document_ids": [*seeded, missing_id]},
            )
            payload = extract_payload(response)

            assert payload["documents"] == seeded
            assert missing_id in payload["errors"]
        finally:
            await _cleanup(session, seeded)


@pytest.mark.asyncio
async def test_all_ids_missing_returns_empty_documents_not_a_failure() -> None:
    async with create_mcp_session() as session:
        missing = [f"test_bulk_absent_{uuid.uuid4().hex[:8]}" for _ in range(3)]

        response = await session.call_tool(
            "get_documents_by_ids",
            arguments={**_keyspace(), "document_ids": missing},
        )
        payload = extract_payload(response)

        assert payload["documents"] == {}
        assert set(payload["errors"]) == set(missing)


@pytest.mark.asyncio
async def test_duplicate_ids_are_returned_once() -> None:
    """Documented behaviour: the result is keyed by ID, so duplicates collapse."""
    async with create_mcp_session() as session:
        seeded = await _seed(session, 1)
        doc_id = next(iter(seeded))
        try:
            response = await session.call_tool(
                "get_documents_by_ids",
                arguments={**_keyspace(), "document_ids": [doc_id, doc_id]},
            )
            payload = extract_payload(response)
            assert payload["documents"] == seeded
        finally:
            await _cleanup(session, seeded)


@pytest.mark.asyncio
async def test_empty_id_list_is_rejected() -> None:
    async with create_mcp_session() as session:
        response = await session.call_tool(
            "get_documents_by_ids",
            arguments={**_keyspace(), "document_ids": []},
        )
        assert "error" in extract_payload(response)


@pytest.mark.asyncio
async def test_oversized_batch_is_rejected() -> None:
    async with create_mcp_session() as session:
        too_many = [f"doc{n}" for n in range(MAX_BULK_GET_IDS + 1)]

        response = await session.call_tool(
            "get_documents_by_ids",
            arguments={**_keyspace(), "document_ids": too_many},
        )
        payload = extract_payload(response)
        assert str(MAX_BULK_GET_IDS) in payload["error"]


@pytest.mark.asyncio
async def test_still_available_when_the_server_runs_read_only() -> None:
    """It is a read tool, so read-only mode must not filter it out. The session
    is started with read-only mode explicitly ON - the default test session
    forces it off, which would make this assertion meaningless."""
    async with create_mcp_session(extra_env=READ_ONLY_ENV) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}

        assert "get_documents_by_ids" in names
        assert "upsert_document_by_id" not in names, (
            "sanity check: read-only mode is not actually in effect"
        )


@pytest.mark.asyncio
async def test_reads_real_data_in_read_only_mode() -> None:
    """Listing the tool is not the same as it working under that mode."""
    async with create_mcp_session() as session:
        seeded = await _seed(session, 2)

    try:
        async with create_mcp_session(extra_env=READ_ONLY_ENV) as session:
            response = await session.call_tool(
                "get_documents_by_ids",
                arguments={**_keyspace(), "document_ids": list(seeded)},
            )
            assert extract_payload(response)["documents"] == seeded
    finally:
        async with create_mcp_session() as session:
            await _cleanup(session, seeded)
