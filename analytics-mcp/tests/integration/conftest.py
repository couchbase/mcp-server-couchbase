"""Shared fixtures/utilities for the EA prototype server's integration tests.

Trimmed down from the parent mcp-server-couchbase's tests/integration/conftest.py:
stdio transport only (no http/sse/OAuth branches), since this server has none of
that. Spawns a fresh ``ea_mcp_server`` subprocess per test session.

Connection defaults point at the local Docker Enterprise Analytics cluster set
up per https://docs.couchbase.com/enterprise-analytics/current/intro/do-a-quick-install.html
(``docker run ... -p 8091:8091 -p 8095:8095 couchbase/enterprise-analytics:2.2.0``,
initialized with Administrator/password). Port 8095 (the Analytics service's
REST/query port) is confirmed to be the right SDK connection target for this
topology -- ``Cluster.create_instance("http://localhost:8095", ...)`` connects
and runs queries successfully.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters, stdio_client

DEFAULT_CONNECTION_STRING = "http://localhost:8095"
DEFAULT_USERNAME = "Administrator"
DEFAULT_PASSWORD = "password"


def _build_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("EA_CONNECTION_STRING", DEFAULT_CONNECTION_STRING)
    env.setdefault("EA_USERNAME", DEFAULT_USERNAME)
    env.setdefault("EA_PASSWORD", DEFAULT_PASSWORD)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


@asynccontextmanager
async def create_mcp_session() -> AsyncIterator[ClientSession]:
    """Spawn a fresh ``ea_mcp_server`` subprocess and yield a session to it."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ea_mcp_server"],
        env=_build_subprocess_env(),
    )
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


def extract_payload(response):
    """Extract a usable payload from a tool response.

    Copied from the parent repo's tests/integration/conftest.py -- MCP tool
    responses can be a single JSON-encoded content block, or multiple content
    blocks (one per list item for list returns).
    """
    content = getattr(response, "content", None) or []
    if not content:
        return None

    if len(content) > 1:
        items = []
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                try:
                    items.append(json.loads(text))
                except json.JSONDecodeError:
                    items.append(text)
        return items if items else None

    first = content[0]
    raw = getattr(first, "text", None)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw
