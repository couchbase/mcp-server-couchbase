"""
Integration tests for the scope/collection management tools.

Tests for:
- create_scope / delete_scope
- create_collection / delete_collection
- error envelopes for already-exists / not-found conditions

Verification uses get_scopes_and_collections_in_bucket (SDK collection manager,
immediately consistent) rather than the query-service-backed
get_collections_in_scope, to avoid races on newly-created keyspaces.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import (
    create_mcp_session,
    extract_payload,
    require_test_bucket,
)


async def _scopes_and_collections(session, bucket: str) -> dict:
    payload = extract_payload(
        await session.call_tool(
            "get_scopes_and_collections_in_bucket",
            arguments={"bucket_name": bucket},
        )
    )
    return payload if isinstance(payload, dict) else {}


async def _delete_scope_quietly(session, bucket: str, scope: str) -> None:
    """Best-effort teardown. delete_scope returns an error envelope (never
    raises) when the scope is already gone, so this is safe to always call."""
    await session.call_tool(
        "delete_scope",
        arguments={"bucket_name": bucket, "scope_name": scope},
    )


@pytest.mark.asyncio
async def test_create_and_delete_scope() -> None:
    """create_scope adds a scope visible in the bucket; delete_scope removes it."""
    bucket = require_test_bucket()
    scope = f"test_scope_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            created = extract_payload(
                await session.call_tool(
                    "create_scope",
                    arguments={"bucket_name": bucket, "scope_name": scope},
                )
            )
            assert created["success"] is True, f"create_scope failed: {created}"
            assert scope in await _scopes_and_collections(session, bucket)

            deleted = extract_payload(
                await session.call_tool(
                    "delete_scope",
                    arguments={"bucket_name": bucket, "scope_name": scope},
                )
            )
            assert deleted["success"] is True, f"delete_scope failed: {deleted}"
            assert scope not in await _scopes_and_collections(session, bucket)
        finally:
            await _delete_scope_quietly(session, bucket, scope)


@pytest.mark.asyncio
async def test_create_and_delete_collection() -> None:
    """create_collection adds a collection to a scope; delete_collection removes it."""
    bucket = require_test_bucket()
    scope = f"test_scope_{uuid.uuid4().hex[:8]}"
    collection = f"test_col_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            assert (
                extract_payload(
                    await session.call_tool(
                        "create_scope",
                        arguments={"bucket_name": bucket, "scope_name": scope},
                    )
                )["success"]
                is True
            )

            created = extract_payload(
                await session.call_tool(
                    "create_collection",
                    arguments={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                    },
                )
            )
            assert created["success"] is True, f"create_collection failed: {created}"
            assert collection in (await _scopes_and_collections(session, bucket)).get(
                scope, []
            )

            deleted = extract_payload(
                await session.call_tool(
                    "delete_collection",
                    arguments={
                        "bucket_name": bucket,
                        "scope_name": scope,
                        "collection_name": collection,
                    },
                )
            )
            assert deleted["success"] is True, f"delete_collection failed: {deleted}"
            assert collection not in (
                await _scopes_and_collections(session, bucket)
            ).get(scope, [])
        finally:
            await _delete_scope_quietly(session, bucket, scope)


@pytest.mark.asyncio
async def test_create_scope_already_exists_returns_error() -> None:
    """Recreating an existing scope returns a log-and-return error envelope."""
    bucket = require_test_bucket()
    scope = f"test_scope_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        try:
            first = extract_payload(
                await session.call_tool(
                    "create_scope",
                    arguments={"bucket_name": bucket, "scope_name": scope},
                )
            )
            assert first["success"] is True

            duplicate = extract_payload(
                await session.call_tool(
                    "create_scope",
                    arguments={"bucket_name": bucket, "scope_name": scope},
                )
            )
            assert duplicate["success"] is False
            assert "error" in duplicate
        finally:
            await _delete_scope_quietly(session, bucket, scope)


@pytest.mark.asyncio
async def test_delete_nonexistent_scope_returns_error() -> None:
    """Deleting a scope that does not exist returns an error envelope, not a raise."""
    bucket = require_test_bucket()
    scope = f"missing_scope_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        result = extract_payload(
            await session.call_tool(
                "delete_scope",
                arguments={"bucket_name": bucket, "scope_name": scope},
            )
        )
        assert result["success"] is False
        assert "error" in result


@pytest.mark.asyncio
async def test_create_collection_in_missing_scope_returns_error() -> None:
    """Creating a collection in a nonexistent scope returns an error envelope."""
    bucket = require_test_bucket()
    scope = f"missing_scope_{uuid.uuid4().hex[:8]}"
    collection = f"test_col_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        result = extract_payload(
            await session.call_tool(
                "create_collection",
                arguments={
                    "bucket_name": bucket,
                    "scope_name": scope,
                    "collection_name": collection,
                },
            )
        )
        assert result["success"] is False
        assert "error" in result
