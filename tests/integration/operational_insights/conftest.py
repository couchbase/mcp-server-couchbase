"""Shared fixtures/utilities for the Operational Insights integration tests.

Everything under this directory is auto-tagged ``integration`` and
``operational_insights``, and skipped at collection time unless
``CB_OI_CONNECTION_STRING``/``CB_OI_USERNAME``/``CB_OI_PASSWORD`` are set.
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
from _test_env import build_oi_env, oi_env_available
from conftest import create_session_for_subcommand
from mcp import ClientSession


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(
        reason=(
            "Operational Insights integration tests require a live cluster. "
            "Set CB_OI_CONNECTION_STRING/CB_OI_USERNAME/CB_OI_PASSWORD."
        )
    )
    for item in items:
        if "tests/integration/operational_insights" in str(item.fspath).replace(
            os.sep, "/"
        ):
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.operational_insights)
            if not oi_env_available():
                item.add_marker(skip)


@asynccontextmanager
async def create_oi_mcp_session() -> AsyncIterator[ClientSession]:
    """Spawn a fresh ``mcp_server operational-insights`` subprocess."""
    env = build_oi_env()
    async with create_session_for_subcommand(
        "operational-insights", env
    ) as session:
        yield session
