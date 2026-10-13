"""
Integration tests for the opt-in replica read fallback on get_document_by_id.

A healthy cluster cannot exercise the fallback itself, because the active copy
answers and no replica is ever consulted. What can be proved against a live
server, and what these tests prove, is the contract around it: the default read
is unchanged, the flag changes only the shape, and a missing document is still
reported as missing rather than being turned into a replica round trip.
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


def _keyspace() -> dict[str, str]:
    return {
        "bucket_name": require_test_bucket(),
        "scope_name": get_test_scope(),
        "collection_name": get_test_collection(),
    }


async def _seed(session, content: dict) -> str:
    doc_id = f"test_replica_{uuid.uuid4().hex[:8]}"
    await session.call_tool(
        "upsert_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id, "document_content": content},
    )
    return doc_id


async def _delete(session, doc_id: str) -> None:
    await session.call_tool(
        "delete_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id},
    )


@pytest.mark.asyncio
async def test_default_read_is_unchanged() -> None:
    """A caller that does not ask for the fallback sees the bare document."""
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"name": "plain", "value": 1})
        try:
            response = await session.call_tool(
                "get_document_by_id",
                arguments={**_keyspace(), "document_id": doc_id},
            )
            assert extract_payload(response) == {"name": "plain", "value": 1}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_fallback_flag_reports_an_authoritative_read_on_a_healthy_cluster() -> (
    None
):
    """With the flag set and the active copy reachable, the read comes from the
    active copy and says so, so the caller can tell it is not a stale replica."""
    async with create_mcp_session() as session:
        doc_id = await _seed(session, {"name": "fresh", "value": 2})
        try:
            response = await session.call_tool(
                "get_document_by_id",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "allow_replica_fallback": True,
                },
            )
            assert not is_error_response(response)
            payload = extract_payload(response)
            assert payload == {
                "content": {"name": "fresh", "value": 2},
                "is_replica": False,
            }
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_fallback_flag_does_not_hide_a_missing_document() -> None:
    """Not found is an answer, not an outage. The flag must not turn it into a
    replica lookup or a success."""
    async with create_mcp_session() as session:
        doc_id = f"test_replica_missing_{uuid.uuid4().hex[:8]}"
        response = await session.call_tool(
            "get_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "allow_replica_fallback": True,
            },
        )
        assert is_error_response(response), (
            "A missing document must still be an error with the fallback enabled"
        )
