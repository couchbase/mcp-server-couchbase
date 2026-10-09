"""Fixtures for the Operational Insights accuracy tests.

Everything here exists because the accuracy fixtures one level up
(``tests/accuracy/conftest.py``) are bound to the *operational* server:
they spawn ``python -m mcp_server`` with no subcommand, gate on
``CB_CONNECTION_STRING``, and tell the agent to default to a
bucket/scope/collection. The Operational Insights server is a different
process (``mcp_server operational-insights``), different credentials
(``CB_OI_*``), and a different vocabulary (database/scope/collection, no
buckets) — so this conftest overrides the three fixtures that carry those
assumptions and leaves the rest (``judge``, ``result_storage``,
``openai_model``, ``accuracy_run_id``, ``commit_sha``) inherited unchanged.

Skipping policy mirrors ``tests/integration/operational_insights/conftest.py``:
the whole directory is skipped at collection time unless the OI credentials
are set, so a developer (or CI cell) with only a Couchbase Server cluster
sees these reported as skipped rather than failed. The OpenAI key is still
required on top of that, and is enforced by the inherited ``_require_openai``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from _test_env import (
    build_oi_env,
    get_oi_test_collection,
    get_oi_test_database,
    get_oi_test_scope,
    oi_env_available,
)
from mcp import ClientSession, StdioServerParameters, stdio_client

from accuracy.sdk import AccuracyTestingClient, OpenAIAgent

from ..conftest import DEFAULT_TIMEOUT, _require_openai

_OI_ACCURACY_DIR = "tests/accuracy/operational_insights"
_OI_RESULT_VALIDATION_DIR = f"{_OI_ACCURACY_DIR}/result_validation"


def pytest_collection_modifyitems(config, items):
    """Tag this directory ``operational_insights`` and skip it without creds.

    The parent accuracy conftest adds ``accuracy`` to everything under
    tests/accuracy/, so that marker is already handled. ``result_eval`` is
    not: the parent matches one hardcoded path
    (``tests/accuracy/result_validation``), which this tier's own
    ``result_validation`` subdirectory is not under. So it is applied here,
    alongside the OI marker and the OI credential gate.
    """
    skip = pytest.mark.skip(
        reason=(
            "Operational Insights accuracy tests require a live OI cluster. "
            "Set CB_OI_CONNECTION_STRING/CB_OI_USERNAME/CB_OI_PASSWORD."
        )
    )
    available = oi_env_available()
    for item in items:
        path = str(item.fspath).replace(os.sep, "/")
        if _OI_ACCURACY_DIR not in path:
            continue
        item.add_marker(pytest.mark.operational_insights)
        if _OI_RESULT_VALIDATION_DIR in path:
            item.add_marker(pytest.mark.result_eval)
        if not available:
            item.add_marker(skip)


#: Fallback keyspace for the cases that need a collection of real documents
#: they did not create themselves — currently only the schema-inference ones,
#: since ``oi_get_schema_for_collection`` samples actual document content.
#:
#: travel-sample is the sample dataset the OI docs walk you through loading,
#: and ``inventory.airline`` is its most stable collection: a flat, fully
#: populated set of documents whose fields (name, country, callsign, iata,
#: icao, id, type) have not changed across releases. Pointing at it by default
#: means a developer with the standard sample data loaded runs these cases
#: without setting anything.
#:
#: Overridable: ``CB_OI_TEST_DATABASE`` / ``CB_OI_TEST_SCOPE`` /
#: ``CB_OI_TEST_COLLECTION`` still win, for a cluster without travel-sample
#: or one where a different collection is more representative. Those vars
#: also drive the integration tier, where ``Default``/``Default`` is the
#: right default — so the override is read here rather than changing
#: ``_test_env``'s shared helpers.
TRAVEL_SAMPLE_DATABASE = "travel-sample"
TRAVEL_SAMPLE_SCOPE = "inventory"
TRAVEL_SAMPLE_COLLECTION = "airline"


@pytest.fixture()
def oi_database() -> str:
    return get_oi_test_database()


@pytest.fixture()
def oi_scope() -> str:
    return get_oi_test_scope()


@pytest.fixture()
def oi_collection() -> str | None:
    """The collection named by ``CB_OI_TEST_COLLECTION``, or None if unset.

    Kept nullable so a case can tell "explicitly configured" from "falling
    back"; cases that just need *a* populated collection should use
    ``schema_keyspace`` instead.
    """
    return get_oi_test_collection()


@pytest.fixture()
def schema_keyspace(oi_collection: str | None) -> tuple[str, str, str]:
    """A (database, scope, collection) holding real documents to infer from.

    Returns the ``CB_OI_TEST_*`` trio when a collection is configured, and
    otherwise falls back to travel-sample's ``inventory.airline``. The
    fallback is deliberately all-or-nothing: mixing a configured database
    with travel-sample's scope would name a keyspace that exists on neither
    cluster.
    """
    if oi_collection:
        return get_oi_test_database(), get_oi_test_scope(), oi_collection
    return (
        TRAVEL_SAMPLE_DATABASE,
        TRAVEL_SAMPLE_SCOPE,
        TRAVEL_SAMPLE_COLLECTION,
    )


@pytest.fixture()
def openai_agent(openai_model: str, oi_database: str, oi_scope: str) -> OpenAIAgent:
    """Override the operational agent: OI has databases, not buckets.

    The parent fixture's system prompt names a bucket/scope/collection, which
    would push the model toward operational-server parameter names that no
    ``oi_*`` tool accepts.
    """
    api_key = _require_openai()
    base_url = os.getenv("CB_ACCURACY_OPENAI_BASE_URL")
    extra_prompt = (
        "This is a Couchbase Enterprise Analytics / Operational Insights "
        "cluster. It is organised into databases, scopes and collections — "
        "there are no buckets. When the user does not name them explicitly, "
        f"default to database='{oi_database}', scope='{oi_scope}'."
    )
    return OpenAIAgent(
        model=openai_model,
        api_key=api_key,
        base_url=base_url,
        extra_system_prompt=extra_prompt,
    )


@asynccontextmanager
async def _create_oi_mcp_session() -> AsyncIterator[ClientSession]:
    """Spawn ``mcp_server operational-insights`` over stdio and yield a session.

    Deliberately does not reuse ``tests/integration/conftest.py``'s
    ``create_session_for_subcommand``: importing the integration conftest
    from the accuracy tier would pull in that package's collection hooks.
    The spawn itself is four lines, so it is duplicated rather than shared.
    """
    env = build_oi_env()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server", "operational-insights"],
        env=env,
    )
    async with asyncio.timeout(DEFAULT_TIMEOUT):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def create_oi_accuracy_client() -> AsyncIterator[AccuracyTestingClient]:
    """Open an OI MCP session and wrap it for accuracy testing.

    A context manager (not an async-generator fixture) for the same reason
    as the operational one: the MCP/anyio task group must be entered and
    exited on the same asyncio Task as the test body.
    """
    _require_openai()
    async with _create_oi_mcp_session() as session:
        yield AccuracyTestingClient(session)


@pytest.fixture()
def accuracy_client():
    """Override the operational client factory with the OI one.

    Consumed exactly like the parent fixture::

        async with accuracy_client() as client:
            ...
    """
    return create_oi_accuracy_client
