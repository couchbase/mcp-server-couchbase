"""Seed / cleanup hooks for the Operational Insights accuracy cases.

Kept separate from ``accuracy/sdk/seeding.py``: that module's helpers speak
buckets and call operational tool names (``create_scope``,
``upsert_document_by_id``, ...), none of which the OI server registers. Every
hook here goes through ``oi_run_query_sync`` DDL/DML instead, which is the
same approach the OI integration tests take.

All hooks use ``call_tool_silent`` so setup and teardown never land in the
recorded LLM tool-call log — otherwise seeding a collection would itself
count as the model "calling" oi_run_query_sync and inflate every score.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable

from accuracy.sdk.client import AccuracyTestingClient

SetupHook = Callable[[AccuracyTestingClient], Awaitable[None]]


def unique_name(prefix: str) -> str:
    """A unique-per-run identifier, e.g. ``oiacc_scope_1a2b3c4d``.

    Every case that creates server-side state names it through this, so two
    runs (or a CI matrix cell running in parallel with a local run against
    the same cluster) cannot collide.
    """
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


async def _run(client: AccuracyTestingClient, statement: str) -> None:
    await client.call_tool_silent("oi_run_query_sync", {"statement": statement})


def seed_scope(database: str, scope: str) -> SetupHook:
    """Return a hook that creates ``scope`` in ``database`` (silently)."""

    async def _hook(client: AccuracyTestingClient) -> None:
        await _run(client, f"CREATE SCOPE `{database}`.`{scope}` IF NOT EXISTS;")

    return _hook


def drop_scope(database: str, scope: str) -> SetupHook:
    """Return a hook that drops ``scope`` (silently, ignoring absence).

    Dropping the scope also drops its collections and their indexes, so this
    is the only teardown a case needs no matter what it created inside.
    """

    async def _hook(client: AccuracyTestingClient) -> None:
        await _run(client, f"DROP SCOPE `{database}`.`{scope}` IF EXISTS;")

    return _hook


def seed_collection(
    database: str,
    scope: str,
    collection: str,
    *,
    documents: list[dict] | None = None,
) -> SetupHook:
    """Create ``scope``.``collection`` and optionally INSERT ``documents``.

    The collection is declared ``PRIMARY KEY (id: string)``, matching the OI
    integration tests, so seeded documents need an ``id`` field. Documents are
    inserted in one statement so the seed costs a single round trip.
    """

    async def _hook(client: AccuracyTestingClient) -> None:
        await _run(client, f"CREATE SCOPE `{database}`.`{scope}` IF NOT EXISTS;")
        await _run(
            client,
            f"CREATE COLLECTION `{database}`.`{scope}`.`{collection}` "
            "IF NOT EXISTS PRIMARY KEY (id: string);",
        )
        if documents:
            values = ", ".join(json.dumps(doc) for doc in documents)
            await _run(
                client,
                f"INSERT INTO `{database}`.`{scope}`.`{collection}` ([{values}]);",
            )

    return _hook


def seed_index(
    database: str,
    scope: str,
    collection: str,
    index_name: str,
    field: str = "name",
    field_type: str = "string",
) -> SetupHook:
    """Create a secondary index, for cases that must find an existing one.

    Goes through DDL rather than ``oi_create_index`` so that seeding a
    precondition never depends on the very tool a case is scoring.
    """

    async def _hook(client: AccuracyTestingClient) -> None:
        await _run(
            client,
            f"CREATE INDEX `{index_name}` IF NOT EXISTS "
            f"ON `{database}`.`{scope}`.`{collection}` ({field}: {field_type});",
        )

    return _hook
