"""
End-to-end check that scope-filtered tool listing leaves unauthenticated
deployments alone.

The filter applies only when OAuth is active. A stdio or otherwise
unauthenticated server must list exactly what it lists without the filter, or
every existing client would lose tools it is entitled to. The scope-by-scope
behaviour itself needs a token issuer and is covered by the unit tests.
"""

from __future__ import annotations

import pytest
from conftest import create_logging_test_session


async def _names(env: dict[str, str]) -> set[str]:
    async with create_logging_test_session(env_overrides=env) as session:
        return {tool.name for tool in (await session.list_tools()).tools}


@pytest.mark.asyncio
async def test_without_oauth_write_tools_are_listed_when_writes_are_enabled() -> None:
    names = await _names({"CB_MCP_READ_ONLY_MODE": "false"})
    assert {
        "upsert_document_by_id",
        "delete_document_by_id",
        "get_document_by_id",
    } <= names


@pytest.mark.asyncio
async def test_without_oauth_read_only_mode_still_governs_the_list() -> None:
    names = await _names({"CB_MCP_READ_ONLY_MODE": "true"})
    assert "get_document_by_id" in names
    assert "upsert_document_by_id" not in names
