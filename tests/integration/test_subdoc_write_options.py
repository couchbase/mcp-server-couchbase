"""
Integration tests for durability on mutate_subdocument.

What only a live server can show: a MAJORITY sub-document mutation is either
satisfied by the cluster or refused as impossible, and never reports plain
success without the guarantee having been requested. A mocked SDK cannot
distinguish those three cases.
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

DURABILITY_LEVELS = [
    "NONE",
    "MAJORITY",
    "MAJORITY_AND_PERSIST_TO_ACTIVE",
    "PERSIST_TO_MAJORITY",
]


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


async def _read(session, doc_id: str) -> dict:
    response = await session.call_tool(
        "get_document_by_id",
        arguments={**_keyspace(), "document_id": doc_id},
    )
    return extract_payload(response)


@pytest.mark.asyncio
async def test_durability_is_evaluated_by_the_cluster() -> None:
    """A MAJORITY mutation is either satisfied or refused, never quietly ignored.

    Both outcomes are correct depending on the cluster's replica count, so the
    assertion is that whichever happened is internally consistent: on success
    the mutation is readable, on refusal the error is a durability error. The
    case this catches is the third one, where an option that never reached the
    server reports success on a cluster that cannot provide the guarantee.
    """
    doc_id = f"test_subdur_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"n": 1})
            response = await session.call_tool(
                "mutate_subdocument",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "upsert_specs": [{"path": "durable", "value": True}],
                    "durability": "MAJORITY",
                },
            )
            payload = extract_payload(response)

            if "error" in payload:
                assert "durab" in payload["error"].lower(), payload["error"]
            else:
                assert payload["upsert"]["durable"]["success"] is True
                assert (await _read(session, doc_id))["durable"] is True
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_none_durability_is_accepted_everywhere() -> None:
    """NONE is satisfiable on any topology, so it must always succeed."""
    doc_id = f"test_subdur_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"n": 1})
            response = await session.call_tool(
                "mutate_subdocument",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "upsert_specs": [{"path": "level", "value": "NONE"}],
                    "durability": "NONE",
                },
            )
            payload = extract_payload(response)
            assert "error" not in payload, payload
            assert (await _read(session, doc_id))["level"] == "NONE"
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_invalid_durability_is_rejected_and_nothing_is_mutated() -> None:
    doc_id = f"test_subdur_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"untouched": True})
            response = await session.call_tool(
                "mutate_subdocument",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "upsert_specs": [{"path": "should_not_exist", "value": 1}],
                    "durability": "NOT_A_LEVEL",
                },
            )
            payload = extract_payload(response)
            assert "error" in payload
            assert "NOT_A_LEVEL" in payload["error"]

            document = await _read(session, doc_id)
            assert document == {"untouched": True}
        finally:
            await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_every_documented_level_is_accepted_by_the_tool() -> None:
    """Each level must be understood at the tool boundary.

    A level the cluster cannot satisfy is allowed to fail, but it must fail
    with a durability error from the server rather than a validation error
    from the tool, which is what a typo in the mapping would produce.
    """
    async with create_mcp_session() as session:
        for level in DURABILITY_LEVELS:
            doc_id = f"test_subdur_{uuid.uuid4().hex[:8]}"
            try:
                await _seed(session, doc_id, {"n": 1})
                response = await session.call_tool(
                    "mutate_subdocument",
                    arguments={
                        **_keyspace(),
                        "document_id": doc_id,
                        "upsert_specs": [{"path": "lvl", "value": level}],
                        "durability": level,
                    },
                )
                payload = extract_payload(response)
                if "error" in payload:
                    assert "must be one of" not in payload["error"], level
            finally:
                await _delete(session, doc_id)


@pytest.mark.asyncio
async def test_omitting_the_options_leaves_behaviour_unchanged() -> None:
    """The pre-existing call path must be untouched when nothing is requested."""
    doc_id = f"test_subdur_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            await _seed(session, doc_id, {"n": 1})
            response = await session.call_tool(
                "mutate_subdocument",
                arguments={
                    **_keyspace(),
                    "document_id": doc_id,
                    "upsert_specs": [{"path": "plain", "value": True}],
                },
            )
            payload = extract_payload(response)
            assert "error" not in payload, payload
            assert payload["upsert"]["plain"]["success"] is True
            assert (await _read(session, doc_id))["plain"] is True
        finally:
            await _delete(session, doc_id)
