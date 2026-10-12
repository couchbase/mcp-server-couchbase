"""Shared fixtures/utilities for the Operational Insights integration tests.

Everything under this directory is auto-tagged ``integration`` and
``operational_insights``, and skipped at collection time unless
``CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING``/``CB_OPERATIONAL_INSIGHTS_USERNAME``/``CB_OPERATIONAL_INSIGHTS_PASSWORD`` are set.
This is what keeps every operational CI cell green: with none of those set,
the whole directory reports skipped, not failed.

Connection target: a local Docker Operational Insights cluster set up per
https://docs.couchbase.com/enterprise-analytics/current/intro/do-a-quick-install.html
(``couchbase/enterprise-analytics:2.2.x`` + an ``adobe/s3mock`` sidecar on a
shared docker network, initialized with Administrator/password). Port 8095
is the confirmed SDK connection target — see
``cb_mcp.utils.operational_insights.connection``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from _test_env import build_operational_insights_env, operational_insights_env_available
from conftest import create_session_for_subcommand, streamable_http_session
from mcp import ClientSession


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(
        reason=(
            "Operational Insights integration tests require a live cluster. "
            "Set CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING/CB_OPERATIONAL_INSIGHTS_USERNAME/CB_OPERATIONAL_INSIGHTS_PASSWORD."
        )
    )
    for item in items:
        if "tests/integration/operational_insights" in str(item.fspath).replace(
            os.sep, "/"
        ):
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.operational_insights)
            if not operational_insights_env_available():
                item.add_marker(skip)


@asynccontextmanager
async def create_operational_insights_mcp_session() -> AsyncIterator[ClientSession]:
    """Create a fresh Operational Insights MCP client session.

    Transport selection mirrors ``tests/integration/conftest.py``'s
    ``create_mcp_session`` (driven by ``CB_MCP_TRANSPORT``, default
    ``stdio``):

    - ``stdio``: spawn a fresh ``mcp_server operational-insights``
      subprocess per test. ``build_operational_insights_env()`` supplies Operational Insights credentials and
      forces the subprocess's own transport to stdio.
    - ``http`` / ``streamable-http``: connect to an already-running server
      at ``MCP_SERVER_URL``, started outside pytest (by CI or
      ``scripts/run_operational_insights_matrix_local.sh``) with ``operational-insights`` as
      its subcommand. Reuses ``streamable_http_session`` unmodified — it
      is already fully generic (reads only ``MCP_SERVER_URL``), so there is
      no operational-only logic to fork.
    """
    transport = os.getenv("CB_MCP_TRANSPORT", "stdio").lower()

    if transport in ("http", "streamable-http"):
        async with streamable_http_session() as session:
            yield session
        return

    env = build_operational_insights_env()
    async with create_session_for_subcommand("operational-insights", env) as session:
        yield session
