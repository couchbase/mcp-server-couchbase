"""Which deployment the integration run targets, and what that withholds.

On Capella the server does not register the on-prem-only tools at all, so a
call to one fails with MCP's generic "Unknown tool" error, not with the tool's
own Capella rejection. CI only runs against self-managed containers, so these
helpers matter for manual Capella runs: registration tests narrow what they
expect, and tests that call a withheld tool skip instead of failing.

The deployment is read with the server's own ``resolve_deployment`` so the
tests and the server cannot disagree about which tools are registered. A
Capella cluster behind a custom DNS name resolves as on-prem here and in the
server alike; the tools then reject the call themselves, which the existing
"Capella" error branches in the tests still cover.
"""

from __future__ import annotations

import os

import pytest

from cb_mcp.core.spec import Deployment
from cb_mcp.utils.operational.connection_string import resolve_deployment

from ._census import ON_PREM_ONLY_TOOLS


def targets_capella() -> bool:
    """True when ``CB_CONNECTION_STRING`` names a Capella cluster."""
    connection_string = os.getenv("CB_CONNECTION_STRING")
    return resolve_deployment(connection_string) is Deployment.CAPELLA


def expected_on_target(tools: set[str] | frozenset[str]) -> set[str]:
    """*tools*, minus the ones the server withholds on the targeted deployment."""
    if targets_capella():
        return set(tools) - ON_PREM_ONLY_TOOLS
    return set(tools)


def skip_if_capella(tool_name: str) -> None:
    """Skip a test that calls *tool_name* when the server withholds it."""
    if tool_name in ON_PREM_ONLY_TOOLS and targets_capella():
        pytest.skip(f"{tool_name} is on-prem only and not registered on Capella")
