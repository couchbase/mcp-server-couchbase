"""
Integration tests for kv.py tools.

Tests for:
- get_document_by_id
- lookup_subdocument
- mutate_subdocument
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

        # upsert returns {"success": True} on success
        assert payload == {"success": True}, (
            f"Expected success on upsert, got {payload}"
        )

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

        # delete returns {"success": True} on success
        assert payload == {"success": True}, (
            f"Expected success on delete, got {payload}"
        )


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

        # insert returns {"success": True} on success
        assert payload == {"success": True}, (
            f"Expected success on insert, got {payload}"
        )

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

        # insert returns {"success": False, "error": ...} when document already exists
        assert payload.get("success") is False, (
            f"Insert should fail when document exists, got {payload}"
        )
        assert "error" in payload

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

        # replace returns {"success": True} on success
        assert payload == {"success": True}, (
            f"Expected success on replace, got {payload}"
        )

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

        # replace returns {"success": False, "error": ...} when document doesn't exist
        assert payload.get("success") is False, (
            f"Replace should fail when document doesn't exist, got {payload}"
        )
        assert "error" in payload


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

        # delete returns {"success": False, "error": ...} when document doesn't exist
        assert payload.get("success") is False, (
            f"Delete should fail when document doesn't exist, got {payload}"
        )
        assert "error" in payload


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


@pytest.mark.asyncio
async def test_lookup_subdocument_get_paths() -> None:
    """Verify lookup_subdocument fetches individual field values."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_subdoc_get_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Subdoc Test", "address": {"city": "Austin"}, "value": 42}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "lookup_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "get_paths": ["name", "address.city"],
            },
        )
        payload = extract_payload(response)

        assert payload["get"]["name"] == {"value": "Subdoc Test"}
        assert payload["get"]["address.city"] == {"value": "Austin"}

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
async def test_lookup_subdocument_exists_paths() -> None:
    """Verify lookup_subdocument checks presence without fetching values."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_subdoc_exists_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Exists Test", "tags": ["a", "b"]}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "lookup_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "exists_paths": ["tags", "nickname"],
            },
        )
        payload = extract_payload(response)

        assert payload["exists"]["tags"] == {"value": True}
        assert payload["exists"]["nickname"] == {"value": False}

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
async def test_lookup_subdocument_count_paths() -> None:
    """Verify lookup_subdocument returns the element count of an array."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_subdoc_count_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Count Test", "tags": ["a", "b", "c"]}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "lookup_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "count_paths": ["tags"],
            },
        )
        payload = extract_payload(response)

        assert payload["count"]["tags"] == {"value": 3}

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
async def test_lookup_subdocument_combined_ops() -> None:
    """Verify get, exists, and count can be combined in a single call."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_subdoc_combined_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Combined Test", "tags": ["a", "b"], "active": True}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "lookup_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "get_paths": ["name"],
                "exists_paths": ["active"],
                "count_paths": ["tags"],
            },
        )
        payload = extract_payload(response)

        assert payload["get"]["name"] == {"value": "Combined Test"}
        assert payload["exists"]["active"] == {"value": True}
        assert payload["count"]["tags"] == {"value": 2}

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
async def test_lookup_subdocument_missing_path_reports_error_not_raise() -> None:
    """A path that doesn't exist on an otherwise-real document must be reported
    per-path as a failure, without failing the other requested paths."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_subdoc_missing_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Partial Failure Test"}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "lookup_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "get_paths": ["name", "does.not.exist"],
            },
        )
        payload = extract_payload(response)
        is_error = getattr(response, "isError", None) or getattr(
            response, "is_error", False
        )

        assert not is_error, "A missing sub-path must not fail the whole call"
        assert payload["get"]["name"] == {"value": "Partial Failure Test"}
        assert "error" in payload["get"]["does.not.exist"]

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
async def test_lookup_subdocument_no_paths_returns_error() -> None:
    """Calling with no get/exists/count paths at all must report an error in the
    payload without failing the MCP call itself."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    doc_id = f"test_subdoc_nopaths_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "lookup_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )
        payload = extract_payload(response)

        is_error = getattr(response, "isError", None) or getattr(
            response, "is_error", False
        )
        assert not is_error
        assert "error" in payload, "Expected an error key when no paths are provided"


@pytest.mark.asyncio
async def test_lookup_subdocument_document_not_found() -> None:
    """A document_id that does not exist must return {"error": ...} without
    raising an MCP-level error."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    doc_id = f"test_subdoc_notfound_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "lookup_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "get_paths": ["name"],
            },
        )
        payload = extract_payload(response)

        is_error = getattr(response, "isError", None) or getattr(
            response, "is_error", False
        )
        assert not is_error
        assert "error" in payload, "Expected an error key when document does not exist"


@pytest.mark.asyncio
async def test_lookup_subdocument_count_on_scalar_reports_error() -> None:
    """count on a scalar field (PathMismatchException) must be reported as a
    per-path error without failing the other requested paths."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_subdoc_countscalar_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Mismatch Test", "tags": ["x", "y"]}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "lookup_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "count_paths": ["name", "tags"],
            },
        )
        payload = extract_payload(response)

        is_error = getattr(response, "isError", None) or getattr(
            response, "is_error", False
        )
        assert not is_error, (
            "PathMismatch on one count path must not fail the whole call"
        )
        assert "error" in payload["count"]["name"], (
            "count on a scalar field must report per-path failure"
        )
        assert payload["count"]["tags"] == {"value": 2}

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
async def test_lookup_subdocument_array_index_paths() -> None:
    """Array index paths (bracket notation and negative indexing) must resolve
    correctly, including nested paths inside array elements."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_subdoc_arrayidx_{uuid.uuid4().hex[:8]}"
    doc_content = {
        "name": "Array Index Test",
        "reviews": [
            {"author": "Alice", "rating": 5},
            {"author": "Bob", "rating": 3},
        ],
    }

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "lookup_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "get_paths": ["reviews[0].author", "reviews[-1].rating"],
            },
        )
        payload = extract_payload(response)

        assert payload["get"]["reviews[0].author"] == {"value": "Alice"}
        assert payload["get"]["reviews[-1].rating"] == {"value": 3}

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
async def test_mutate_subdocument_upsert() -> None:
    """Verify mutate_subdocument can upsert a new field into an existing doc."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_mutate_upsert_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Mutate Upsert Test"}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "upsert_specs": [{"path": "status", "value": "active"}],
            },
        )
        payload = extract_payload(response)

        assert payload["upsert"]["status"] == {"success": True}

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
        assert get_payload["status"] == "active"

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
async def test_mutate_subdocument_insert_fails_if_path_exists() -> None:
    """insert_specs on an already-present path must fail atomically without
    raising an MCP-level error."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_mutate_insert_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Mutate Insert Test", "status": "already-here"}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "insert_specs": [{"path": "status", "value": "should-not-land"}],
            },
        )
        payload = extract_payload(response)

        is_error = getattr(response, "isError", None) or getattr(
            response, "is_error", False
        )
        assert not is_error
        assert "error" in payload

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
        assert get_payload["status"] == "already-here"

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
async def test_mutate_subdocument_array_append_and_prepend() -> None:
    """Verify array_append_specs/array_prepend_specs place values at the
    expected ends of an array."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_mutate_array_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Mutate Array Test", "tags": ["b"]}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "array_append_specs": [{"path": "tags", "values": ["c"]}],
                "array_prepend_specs": [{"path": "tags", "values": ["a"]}],
            },
        )
        payload = extract_payload(response)

        assert payload["array_append"]["tags"] == {"success": True}
        assert payload["array_prepend"]["tags"] == {"success": True}

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
        assert get_payload["tags"] == ["a", "b", "c"]

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
async def test_mutate_subdocument_array_add_unique() -> None:
    """Verify array_add_unique_specs rejects a duplicate scalar and accepts a new one."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_mutate_adduniq_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Mutate Add Unique Test", "tags": ["a", "b"]}

    async with create_mcp_session() as session:
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

        dup_response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "array_add_unique_specs": [{"path": "tags", "value": "a"}],
            },
        )
        dup_payload = extract_payload(dup_response)
        assert "error" in dup_payload, "Adding a duplicate value must fail atomically"

        new_response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "array_add_unique_specs": [{"path": "tags", "value": "c"}],
            },
        )
        new_payload = extract_payload(new_response)
        assert new_payload["array_add_unique"]["tags"] == {"success": True}

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
        assert sorted(get_payload["tags"]) == ["a", "b", "c"]

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
async def test_mutate_subdocument_create_parents() -> None:
    """create_parents=False must fail when the parent path is missing;
    create_parents=True must succeed and create the intermediate path."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_mutate_createparents_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Mutate Create Parents Test"}

    async with create_mcp_session() as session:
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

        no_parents_response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "upsert_specs": [{"path": "address.city", "value": "Austin"}],
                "create_parents": False,
            },
        )
        no_parents_payload = extract_payload(no_parents_response)
        assert "error" in no_parents_payload

        with_parents_response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "upsert_specs": [{"path": "address.city", "value": "Austin"}],
                "create_parents": True,
            },
        )
        with_parents_payload = extract_payload(with_parents_response)
        assert with_parents_payload["upsert"]["address.city"] == {"success": True}

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
        assert get_payload["address"]["city"] == "Austin"

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
async def test_mutate_subdocument_no_specs_returns_error() -> None:
    """Calling with no mutation specs at all must report an error in the
    payload without failing the MCP call itself."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    doc_id = f"test_mutate_nospecs_{uuid.uuid4().hex[:8]}"

    async with create_mcp_session() as session:
        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )
        payload = extract_payload(response)

        is_error = getattr(response, "isError", None) or getattr(
            response, "is_error", False
        )
        assert not is_error
        assert "error" in payload, "Expected an error key when no specs are provided"


@pytest.mark.asyncio
async def test_mutate_subdocument_replace() -> None:
    """Verify mutate_subdocument can replace existing fields."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_mutate_replace_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Test Hotel", "status": "pending"}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "replace_specs": [{"path": "status", "value": "confirmed"}],
            },
        )
        payload = extract_payload(response)

        assert payload["replace"]["status"]["success"] is True

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
        assert get_payload["status"] == "confirmed"

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
async def test_mutate_subdocument_array_append() -> None:
    """Verify mutate_subdocument can append values to arrays."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_mutate_array_append_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Test Hotel", "tags": ["wifi", "parking"]}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "array_append_specs": [{"path": "tags", "values": ["gym", "spa"]}],
            },
        )
        payload = extract_payload(response)

        assert payload["array_append"]["tags"]["success"] is True

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
        assert get_payload["tags"] == ["wifi", "parking", "gym", "spa"]

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
async def test_mutate_subdocument_counter() -> None:
    """Verify mutate_subdocument can increment and decrement counters."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_mutate_counter_{uuid.uuid4().hex[:8]}"
    doc_content = {"name": "Test Hotel", "reviews_count": 0}

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "counter_specs": [{"path": "reviews_count", "delta": 5}],
            },
        )
        payload = extract_payload(response)

        assert payload["counter"]["reviews_count"]["success"] is True
        assert payload["counter"]["reviews_count"]["value"] == 5

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
async def test_mutate_subdocument_remove() -> None:
    """Verify mutate_subdocument can remove fields from documents."""
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()

    doc_id = f"test_mutate_remove_{uuid.uuid4().hex[:8]}"
    doc_content = {
        "name": "Test Hotel",
        "deprecated_field": "should be removed",
    }

    async with create_mcp_session() as session:
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

        response = await session.call_tool(
            "mutate_subdocument",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
                "remove_paths": ["deprecated_field"],
            },
        )
        payload = extract_payload(response)

        assert payload["remove"]["deprecated_field"]["success"] is True

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
        assert "deprecated_field" not in get_payload

        await session.call_tool(
            "delete_document_by_id",
            arguments={
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": doc_id,
            },
        )
