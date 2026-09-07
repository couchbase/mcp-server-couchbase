"""Shared seed / cleanup helpers for accuracy test cases.

These build the async ``seed`` / ``cleanup`` hooks that ``AccuracyCase`` and
``ResultCase`` accept. They use ``call_tool_silent`` so the setup/teardown
KV operations never pollute the recorded LLM tool-call log.

Used by both the tool-calling tests (tests/accuracy/tool_calling/) and the
result-validation tests (tests/accuracy/result_validation/).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from couchbase.management.search import SearchIndex

from cb_mcp.utils.connection import connect_to_couchbase_cluster

from .client import AccuracyTestingClient

SetupHook = Callable[[AccuracyTestingClient], Awaitable[None]]


def unique_name(prefix: str) -> str:
    """A unique-per-run identifier, e.g. ``acc_scope_1a2b3c4d`` — usable as a
    document id, throwaway scope name, or collection name."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# Alias kept for call sites that seed documents — same generator, doc-specific name.
doc_id = unique_name


def seed_scope(bucket: str, scope: str) -> SetupHook:
    """Return a hook that creates ``scope`` in ``bucket`` (silently)."""

    async def _hook(client: AccuracyTestingClient) -> None:
        await client.call_tool_silent(
            "create_scope",
            {"bucket_name": bucket, "scope_name": scope},
        )

    return _hook


def seed_collection(bucket: str, scope: str, collection: str) -> SetupHook:
    """Return a hook that creates ``collection`` in ``scope`` (silently)."""

    async def _hook(client: AccuracyTestingClient) -> None:
        await client.call_tool_silent(
            "create_collection",
            {
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
            },
        )

    return _hook


def drop_scope(bucket: str, scope: str) -> SetupHook:
    """Return a hook that deletes ``scope`` (silently, best-effort).

    Cascades to every collection within the scope, so it doubles as the
    teardown for anything a case created under a throwaway scope.
    """

    async def _hook(client: AccuracyTestingClient) -> None:
        await client.call_tool_silent(
            "delete_scope",
            {"bucket_name": bucket, "scope_name": scope},
        )

    return _hook


def seed_document(
    bucket: str,
    scope: str,
    collection: str,
    document_id: str,
    content: dict[str, Any],
) -> SetupHook:
    """Return a hook that upserts ``content`` at ``document_id`` (silently)."""

    async def _hook(client: AccuracyTestingClient) -> None:
        await client.call_tool_silent(
            "upsert_document_by_id",
            {
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": document_id,
                "document_content": content,
            },
        )

    return _hook


def delete_document(
    bucket: str,
    scope: str,
    collection: str,
    document_id: str,
) -> SetupHook:
    """Return a hook that deletes ``document_id`` (silently, best-effort)."""

    async def _hook(client: AccuracyTestingClient) -> None:
        await client.call_tool_silent(
            "delete_document_by_id",
            {
                "bucket_name": bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": document_id,
            },
        )

    return _hook


def seed_search_index(
    bucket: str, scope: str, collection: str, index_name: str
) -> SetupHook:
    """Return a hook that creates a scope-level Search (FTS) index.

    There is no MCP write tool for Search index management (out of scope for
    that tool family), so unlike every other helper in this module this one
    can't go through ``call_tool_silent`` — it connects to the cluster
    directly via the Couchbase SDK, using the same ``CB_CONNECTION_STRING``/
    ``CB_USERNAME``/``CB_PASSWORD`` env vars the MCP server subprocess uses.
    """

    async def _hook(client: AccuracyTestingClient) -> None:
        cluster = connect_to_couchbase_cluster(
            os.environ["CB_CONNECTION_STRING"],
            os.environ["CB_USERNAME"],
            os.environ["CB_PASSWORD"],
        )
        definition = SearchIndex(
            name=index_name,
            source_type="couchbase",
            idx_type="fulltext-index",
            source_name=bucket,
            params={
                "doc_config": {"mode": "scope.collection.type_field"},
                "mapping": {
                    "types": {
                        f"{scope}.{collection}": {"enabled": True, "dynamic": True}
                    },
                    "default_mapping": {"enabled": False},
                    "default_analyzer": "standard",
                },
            },
        )
        cluster.bucket(bucket).scope(scope).search_indexes().upsert_index(definition)

    return _hook


def drop_search_index(bucket: str, scope: str, index_name: str) -> SetupHook:
    """Return a hook that drops a scope-level Search index (best-effort).

    SDK-direct for the same reason as :func:`seed_search_index`.
    """

    async def _hook(client: AccuracyTestingClient) -> None:
        cluster = connect_to_couchbase_cluster(
            os.environ["CB_CONNECTION_STRING"],
            os.environ["CB_USERNAME"],
            os.environ["CB_PASSWORD"],
        )
        with contextlib.suppress(Exception):
            cluster.bucket(bucket).scope(scope).search_indexes().drop_index(index_name)

    return _hook
