"""
Integration tests for the durability and expiry options on the KV write tools.

These cover what only a live server can show: that a TTL actually expires the
document, and that a durability level is genuinely evaluated by the cluster
rather than accepted and ignored.

The durability test asserts the cluster either satisfies MAJORITY or refuses
it as impossible, without inspecting the topology first - both are correct
answers, and neither is available to an option that was silently dropped.
"""

from __future__ import annotations

import asyncio
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


async def _delete(session, doc_id: str) -> None:
    await session.call_tool(
        "delete_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id},
    )


@pytest.mark.asyncio
async def test_durability_is_evaluated_by_the_cluster() -> None:
    """A MAJORITY write must be either satisfied or refused - never quietly
    downgraded.

    Both outcomes are correct and which one occurs depends on the cluster:
    with a replica the write is acknowledged, without one the server refuses
    it as impossible. What neither outcome allows is the failure this test
    exists to catch - an option that never reaches the server, which would
    report plain success on a cluster that cannot possibly provide the
    guarantee.

    Deliberately does not inspect the cluster's topology first. Ping reports
    the endpoints the SDK currently holds connections to rather than the
    cluster's actual node count, so branching on it produces a test whose
    result depends on connection order.
    """
    doc_id = f"test_dur_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "upsert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": {"durable": True},
                "durability": "MAJORITY",
            },
        )
        payload = extract_payload(response)

        try:
            assert isinstance(payload, dict), (
                f"the tool rejected the call outright rather than running it, "
                f"which usually means the server does not have the durability "
                f"parameter: {payload}"
            )

            if payload["success"]:
                # Satisfied: the document must really be there.
                stored = extract_payload(
                    await session.call_tool(
                        "get_document_by_id",
                        arguments={**_keyspace(), "document_id": doc_id},
                    )
                )
                assert stored == {"durable": True}, (
                    f"durable write reported success but the document is "
                    f"wrong: {stored}"
                )
            else:
                # Refused: it must be refused *for a durability reason*. Any
                # other error means something unrelated broke.
                error = payload.get("error", "")
                assert "durab" in error.lower(), (
                    "expected the cluster either to satisfy MAJORITY or to "
                    f"refuse it as a durability failure, got: {payload}"
                )
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_none_durability_is_accepted_everywhere() -> None:
    """NONE is satisfiable on every topology, so this proves the parameter is
    plumbed through regardless of how the test cluster is built."""
    doc_id = f"test_dur_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "upsert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": {"durable": False},
                "durability": "NONE",
            },
        )
        payload = extract_payload(response)
        try:
            assert payload["success"] is True, (
                f"expected NONE durability to succeed, got {payload}"
            )
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_invalid_durability_is_rejected_and_nothing_is_written() -> None:
    """An unusable option must not degrade into an unguarded write."""
    doc_id = f"test_bad_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "upsert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": {"should_not": "exist"},
                "durability": "MAJORITY_ISH",
            },
        )
        payload = extract_payload(response)
        assert payload["success"] is False
        assert "MAJORITY_ISH" in payload["error"]

        get_response = await session.call_tool(
            "get_document_by_id",
            arguments={**_keyspace(), "document_id": doc_id},
        )
        if not is_error_response(get_response):
            await _delete(session, doc_id)
            pytest.fail("a rejected write must not have created the document")


@pytest.mark.asyncio
async def test_expiry_seconds_actually_expires_the_document() -> None:
    """The server applies expiry lazily, so this polls rather than sleeping
    once for a guessed interval."""
    doc_id = f"test_ttl_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "upsert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": {"transient": True},
                "expiry_seconds": 1,
            },
        )
        assert extract_payload(response)["success"] is True

        for _ in range(20):
            await asyncio.sleep(1)
            get_response = await session.call_tool(
                "get_document_by_id",
                arguments={**_keyspace(), "document_id": doc_id},
            )
            if is_error_response(get_response):
                return

        await _delete(session, doc_id)
        pytest.fail("document with a 1 second TTL was still readable after 20s")


@pytest.mark.asyncio
async def test_insert_accepts_durability_and_expiry() -> None:
    """insert takes the same options as upsert; a divergence here would be a
    silently inconsistent tool surface."""
    doc_id = f"test_ins_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "insert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": {"fresh": True},
                "durability": "NONE",
                "expiry_seconds": 600,
            },
        )
        payload = extract_payload(response)
        try:
            assert payload["success"] is True, payload
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_delete_accepts_durability() -> None:
    doc_id = f"test_del_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": {"doomed": True},
            },
        )
        response = await session.call_tool(
            "delete_document_by_id",
            arguments={**_keyspace(), "document_id": doc_id, "durability": "NONE"},
        )
        assert extract_payload(response)["success"] is True


async def _seed(session, doc_id: str, content: dict, **options) -> dict:
    return extract_payload(
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                **_keyspace(),
                "document_id": doc_id,
                "document_content": content,
                **options,
            },
        )
    )


async def _is_present(session, doc_id: str) -> bool:
    response = await session.call_tool(
        "get_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id},
    )
    return not is_error_response(response)


@pytest.mark.asyncio
async def test_writes_return_the_new_cas_as_a_decimal_string() -> None:
    doc_id = f"test_cas_out_{uuid.uuid4().hex[:8]}"
    async with create_mcp_session() as session:
        try:
            first = await _seed(session, doc_id, {"n": 1})
            second = await _seed(session, doc_id, {"n": 2})
            assert first["cas"].isdigit() and second["cas"].isdigit()
            assert first["cas"] != second["cas"]  # a new revision each write
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_mutate_subdocument_expiry_actually_expires_the_document() -> None:
    doc_id = f"test_mut_exp_{uuid.uuid4().hex[:8]}"
    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"a": 1})
            response = await session.call_tool(
                "mutate_subdocument",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "upsert_specs": [{"path": "a", "value": 2}],
                    "expiry_seconds": 3,
                },
            )
            payload = extract_payload(response)
            assert payload["upsert"]["a"] == {"success": True}
            assert payload["cas"].isdigit()
            assert await _is_present(session, doc_id)
            for _ in range(30):
                if not await _is_present(session, doc_id):
                    break
                await asyncio.sleep(1)
            else:
                pytest.fail("document with a 3s TTL set by mutate_subdocument remained")
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_preserve_expiry_keeps_the_ttl_through_a_write() -> None:
    """A plain upsert clears the TTL; preserve_expiry keeps it, so the document
    still expires."""
    kept = f"test_pres_{uuid.uuid4().hex[:8]}"
    cleared = f"test_pres_clear_{uuid.uuid4().hex[:8]}"
    async with create_mcp_session() as session:
        try:
            for doc_id in (kept, cleared):
                await _seed(session, doc_id, {"n": 1}, expiry_seconds=4)
            await _seed(session, kept, {"n": 2}, preserve_expiry=True)
            await _seed(session, cleared, {"n": 2})  # resets expiry to none
            for _ in range(30):
                if not await _is_present(session, kept):
                    break
                await asyncio.sleep(1)
            else:
                pytest.fail("preserve_expiry did not keep the existing TTL")
            assert await _is_present(session, cleared)
        finally:
            await _delete(session, kept)
            await _delete(session, cleared)


@pytest.mark.asyncio
async def test_expiry_with_preserve_expiry_is_rejected() -> None:
    doc_id = f"test_pres_bad_{uuid.uuid4().hex[:8]}"
    async with create_mcp_session() as session:
        payload = await _seed(
            session, doc_id, {"n": 1}, expiry_seconds=10, preserve_expiry=True
        )
        assert payload["success"] is False
        assert payload["code"] == "invalid_expiry"
        assert not await _is_present(session, doc_id)


@pytest.mark.asyncio
async def test_mutate_subdocument_durability_is_evaluated_by_the_cluster() -> None:
    doc_id = f"test_mut_dur_{uuid.uuid4().hex[:8]}"
    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"a": 1})
            response = await session.call_tool(
                "mutate_subdocument",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "upsert_specs": [{"path": "a", "value": 2}],
                    "durability": "MAJORITY",
                },
            )
            payload = extract_payload(response)
            # Satisfied, or refused as impossible - never silently downgraded.
            assert "cas" in payload or payload.get("code") == "durability_impossible"
        finally:
            await _delete(session, doc_id)
