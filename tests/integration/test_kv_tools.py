"""
Integration tests for kv.py tools.

Tests for:
- get_document_by_id
- upsert_document_by_id
- insert_document_by_id
- replace_document_by_id
- delete_document_by_id
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


@pytest.mark.asyncio
async def test_upsert_document_by_id() -> None:
    """Verify upsert_document_by_id can create a new document."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    # Generate a unique document ID for this test
    doc_id = f"test_doc_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Test Document", "type": "test", "value": 42}

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "upsert_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": doc_content,
            },
        )
        payload = extract_payload(response)

        # upsert returns True on success
        assert payload is True, f"Expected True on upsert success, got {payload}"

        # Clean up: delete the test document
        await session.call_tool(
            "delete_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )


@pytest.mark.asyncio
async def test_get_document_by_id() -> None:
    """Verify get_document_by_id can retrieve a document."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    # Create a test document first
    doc_id = f"test_doc_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Test Get Document", "type": "test", "value": 123}

    async with create_mcp_session() as session:
        # Upsert the document
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": doc_content,
            },
        )

        # Now retrieve it
        response = await session.call_tool(
            "get_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )
        payload = extract_payload(response)

        assert isinstance(payload, dict), f"Expected dict, got {type(payload)}"
        assert payload.get("name") == "Test Get Document"
        assert payload.get("type") == "test"
        assert payload.get("value") == 123

        # Clean up
        await session.call_tool(
            "delete_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )


@pytest.mark.asyncio
async def test_delete_document_by_id() -> None:
    """Verify delete_document_by_id can remove a document."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    # Create a test document first
    doc_id = f"test_doc_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Test Delete Document", "type": "test"}

    async with create_mcp_session() as session:
        # Upsert the document
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": doc_content,
            },
        )

        # Delete it
        response = await session.call_tool(
            "delete_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )
        payload = extract_payload(response)

        # delete returns True on success
        assert payload is True, f"Expected True on delete success, got {payload}"


@pytest.mark.asyncio
async def test_upsert_and_update_document() -> None:
    """Verify upsert_document_by_id can update an existing document."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_doc_{uuid.uuid4().hex[:8]}"
    original_content = {"name": "Original", "version": 1}
    updated_content = {"name": "Updated", "version": 2, "extra_field": "new"}

    async with create_mcp_session() as session:
        # Create original document
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": original_content,
            },
        )

        # Update the document
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": updated_content,
            },
        )

        # Verify the update
        response = await session.call_tool(
            "get_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )
        payload = extract_payload(response)

        assert payload.get("name") == "Updated"
        assert payload.get("version") == 2
        assert payload.get("extra_field") == "new"

        # Clean up
        await session.call_tool(
            "delete_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )


@pytest.mark.asyncio
async def test_insert_document_by_id() -> None:
    """Verify insert_document_by_id can create a new document."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    # Generate a unique document ID for this test
    doc_id = f"test_insert_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Inserted Document", "type": "test", "value": 100}

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "insert_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": doc_content,
            },
        )
        payload = extract_payload(response)

        # insert returns True on success
        assert payload is True, f"Expected True on insert success, got {payload}"

        # Verify the document was created correctly
        get_response = await session.call_tool(
            "get_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )
        get_payload = extract_payload(get_response)
        assert get_payload.get("name") == "Inserted Document"
        assert get_payload.get("value") == 100

        # Clean up: delete the test document
        await session.call_tool(
            "delete_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )


@pytest.mark.asyncio
async def test_insert_document_fails_if_exists() -> None:
    """Verify insert_document_by_id fails when document already exists."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_insert_fail_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Original Document", "type": "test"}

    async with create_mcp_session() as session:
        # First, create the document using upsert
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": doc_content,
            },
        )

        # Now try to insert with the same ID - should fail
        response = await session.call_tool(
            "insert_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": {"name": "Should Not Be Inserted"},
            },
        )
        payload = extract_payload(response)

        # insert returns False when document already exists
        assert payload is False, "Insert should return False when document exists"

        # Clean up
        await session.call_tool(
            "delete_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )


@pytest.mark.asyncio
async def test_replace_document_by_id() -> None:
    """Verify replace_document_by_id can update an existing document."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_replace_{uuid.uuid4().hex[:8]}"
    original_content = {"name": "Original", "version": 1}
    replacement_content = {"name": "Replaced", "version": 2, "replaced": True}

    async with create_mcp_session() as session:
        # First, create the document using upsert
        await session.call_tool(
            "upsert_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": original_content,
            },
        )

        # Now replace the document
        response = await session.call_tool(
            "replace_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": replacement_content,
            },
        )
        payload = extract_payload(response)

        # replace returns True on success
        assert payload is True, f"Expected True on replace success, got {payload}"

        # Verify the replacement
        get_response = await session.call_tool(
            "get_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )
        get_payload = extract_payload(get_response)
        assert get_payload.get("name") == "Replaced"
        assert get_payload.get("version") == 2
        assert get_payload.get("replaced") is True

        # Clean up
        await session.call_tool(
            "delete_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )


@pytest.mark.asyncio
async def test_replace_document_fails_if_not_exists() -> None:
    """Verify replace_document_by_id fails when document does not exist."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    # Use a document ID that definitely doesn't exist
    doc_id = f"test_replace_nonexistent_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        # Try to replace a non-existent document - should fail
        response = await session.call_tool(
            "replace_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": {"name": "Should Not Be Created"},
            },
        )
        payload = extract_payload(response)

        # replace returns False when document doesn't exist
        assert payload is False, (
            "Replace should return False when document doesn't exist"
        )


@pytest.mark.asyncio
async def test_get_document_fails_if_not_exists() -> None:
    """Verify get_document_by_id fails when document does not exist."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    # Use a document ID that definitely doesn't exist
    doc_id = f"test_get_nonexistent_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        # Try to get a non-existent document - should fail with an error
        response = await session.call_tool(
            "get_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )

        # get_document_by_id raises an exception when document doesn't exist,
        # which results in an error response (isError=True in MCP)
        is_error = getattr(response, "isError", None) or getattr(
            response, "is_error", False
        )
        assert is_error is True, (
            "Get should return an error when document doesn't exist"
        )


@pytest.mark.asyncio
async def test_delete_document_fails_if_not_exists() -> None:
    """Verify delete_document_by_id fails when document does not exist."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    # Use a document ID that definitely doesn't exist
    doc_id = f"test_delete_nonexistent_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        # Try to delete a non-existent document - should fail
        response = await session.call_tool(
            "delete_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )
        payload = extract_payload(response)

        # delete returns False when document doesn't exist
        assert payload is False, (
            "Delete should return False when document doesn't exist"
        )


@pytest.mark.asyncio
async def test_upsert_to_nonexistent_bucket_raises_error() -> None:
    """Bug #1: KV write tools should re-raise exceptions, not swallow them.

    Upsert to a bucket that doesn't exist should raise an error, not return False.
    This exposes the bug where all KV write tools catch Exception and return False,
    masking real failures (connection errors, auth errors, bucket not found, etc.).
    """
    scope = get_test_scope()
    collection = get_test_collection()
    doc_id = f"test_doc_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "upsert_document_by_id",
            arguments={
                "bucket_name": "definitely-does-not-exist-xyz123",
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "document_content": {"test": "data"},
            },
        )

        # If the bug exists, this returns False (exception swallowed).
        # If the bug is fixed, this should be an error response.
        payload = extract_payload(response)
        is_error = getattr(response, "isError", None) or getattr(
            response, "is_error", False
        )

        # The CORRECT behavior: error response, NOT False return
        assert is_error is True, (
            f"Upsert to non-existent bucket must raise an error, not return False. "
            f"Got payload={payload}, isError={is_error}. "
            f"This exposes Bug #1: KV tools swallow exceptions."
        )


