"""Deployment gating, end to end over the MCP protocol.

The unit tests in ``tests/unit/operational/test_tool_registration.py`` check
the filter itself. These check the thing an operator actually sees: start the
real server against a connection string, and ask it over the wire which tools
it has.

That distinction matters because registration is only half the story. A tool
can be filtered out of ``prepare_tools_for_registration`` and still be
reachable if anything downstream re-adds it, and a tool missing from
``list_tools`` is not the same as a tool that cannot be called. Both are
asserted here.

No cluster is required: tool registration happens in lifespan startup and the
provider connects lazily, so these pass against a connection string that
points nowhere. They still live in the integration tier because they spawn the
real server process rather than calling a function — and so inherit its
credential skip.
"""

from __future__ import annotations

import pytest
from conftest import create_mcp_session, extract_payload

# Hosts that never resolve. Nothing here connects, and using a real address
# would make a failure look like a network problem.
CAPELLA_CONNECTION_STRING = "couchbases://cb.notarealcluster.cloud.couchbase.com"
SELF_MANAGED_CONNECTION_STRING = "couchbase://cb.notarealcluster.test"

#: Tools that reach Couchbase through an admin REST endpoint Capella does not
#: expose. Mirrors ``TOOL_DEPLOYMENT_REQUIREMENTS``; spelled out rather than
#: imported so a mistaken edit to that mapping fails a test instead of
#: silently redefining what these tests assert.
REST_ONLY_TOOLS = frozenset(
    {
        "get_cluster_metrics",
        "get_cluster_tasks",
        "get_cluster_health_snapshot",
        "get_cluster_system_events",
        "get_index_stats",
        "get_cluster_query_vitals",
        "get_active_queries",
        "delete_active_query",
    }
)

#: Health tools that run through the SDK (ping / diagnostics). These must stay
#: available on Capella — they are the ones an operator still has there.
SDK_HEALTH_TOOLS = frozenset(
    {
        "get_cluster_health_and_services",
        "get_cluster_diagnostics_report",
    }
)


async def _tool_names(connection_string: str, **extra: str) -> set[str]:
    """Tool names the server advertises for *connection_string*."""
    env = {"CB_CONNECTION_STRING": connection_string, **extra}
    async with create_mcp_session(extra_env=env) as session:
        response = await session.list_tools()
        return {tool.name for tool in response.tools}


@pytest.mark.asyncio
async def test_capella_does_not_advertise_rest_only_tools() -> None:
    names = await _tool_names(CAPELLA_CONNECTION_STRING)
    assert not (REST_ONLY_TOOLS & names), (
        f"Capella advertised REST-only tool(s): {sorted(REST_ONLY_TOOLS & names)}"
    )


@pytest.mark.asyncio
async def test_capella_keeps_the_sdk_health_tools() -> None:
    """Ping and diagnostics come from the SDK, so Capella keeps them.

    This is the half of the rule that a too-eager filter would break, and it
    would break quietly — an operator would just find the cluster
    un-diagnosable.
    """
    names = await _tool_names(CAPELLA_CONNECTION_STRING)
    missing = SDK_HEALTH_TOOLS - names
    assert not missing, f"Capella lost SDK-based health tool(s): {sorted(missing)}"


@pytest.mark.asyncio
async def test_self_managed_advertises_every_rest_only_tool() -> None:
    names = await _tool_names(SELF_MANAGED_CONNECTION_STRING)
    missing = REST_ONLY_TOOLS - names
    assert not missing, f"Self-managed is missing tool(s): {sorted(missing)}"


@pytest.mark.asyncio
async def test_capella_withholds_nothing_else() -> None:
    """The gate removes the REST-only tools and exactly those.

    Asserted as a difference between the two deployments rather than against a
    fixed list, so adding a tool to the server does not need an edit here —
    only adding one to the gate does.
    """
    capella = await _tool_names(CAPELLA_CONNECTION_STRING)
    self_managed = await _tool_names(SELF_MANAGED_CONNECTION_STRING)
    assert self_managed - capella == set(REST_ONLY_TOOLS)
    assert not capella - self_managed


@pytest.mark.asyncio
async def test_withheld_tool_cannot_be_called_by_name() -> None:
    """Hidden is not enough: the tool must also be unreachable.

    A client that remembers the name from an earlier self-managed session, or
    an agent that guesses it, must not get through.
    """
    env = {"CB_CONNECTION_STRING": CAPELLA_CONNECTION_STRING}
    async with create_mcp_session(extra_env=env) as session:
        result = await session.call_tool("get_cluster_metrics", {"metrics": []})
        assert result.isError, "Capella accepted a call to a withheld tool"


@pytest.mark.asyncio
async def test_status_tool_reports_withheld_tools_as_disabled() -> None:
    """The withheld set is reported, not silently applied.

    ``get_server_configuration_status`` is the first-line support tool; an
    operator asking "why can't the agent see get_cluster_metrics?" has to be
    able to answer it from here.
    """
    env = {"CB_CONNECTION_STRING": CAPELLA_CONNECTION_STRING}
    async with create_mcp_session(extra_env=env) as session:
        result = await session.call_tool("get_server_configuration_status", {})
        payload = extract_payload(result)
        disabled = set(payload["configuration"]["disabled_tools"])
        assert disabled >= REST_ONLY_TOOLS, (
            f"Status tool did not report withheld tool(s): "
            f"{sorted(REST_ONLY_TOOLS - disabled)}"
        )


@pytest.mark.asyncio
async def test_operator_disabled_tools_are_reported_alongside_withheld_ones() -> None:
    """Both reasons land in one set, and neither erases the other."""
    env = {
        "CB_CONNECTION_STRING": CAPELLA_CONNECTION_STRING,
        "CB_MCP_DISABLED_TOOLS": "get_document_by_id",
    }
    async with create_mcp_session(extra_env=env) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert "get_document_by_id" not in names
        assert not (REST_ONLY_TOOLS & names)

        result = await session.call_tool("get_server_configuration_status", {})
        disabled = set(extract_payload(result)["configuration"]["disabled_tools"])
        assert disabled == REST_ONLY_TOOLS | {"get_document_by_id"}


@pytest.mark.asyncio
async def test_operator_may_disable_a_tool_the_deployment_also_withholds() -> None:
    """Naming an already-withheld tool is not an error and does not double up.

    The parse step validates the operator's list against the full loaded set
    precisely so this stays a legitimate thing to write in a config file.
    """
    env = {
        "CB_CONNECTION_STRING": CAPELLA_CONNECTION_STRING,
        "CB_MCP_DISABLED_TOOLS": "get_cluster_metrics",
    }
    async with create_mcp_session(extra_env=env) as session:
        result = await session.call_tool("get_server_configuration_status", {})
        disabled = set(extract_payload(result)["configuration"]["disabled_tools"])
        assert disabled == REST_ONLY_TOOLS


@pytest.mark.asyncio
async def test_unrecognised_connection_string_withholds_nothing() -> None:
    """No parseable host means "cannot tell", and that must not gate anything.

    The runtime check inside each tool is what covers this case.
    """
    names = await _tool_names("couchbase://")
    missing = REST_ONLY_TOOLS - names
    assert not missing, f"Withheld without a resolved deployment: {sorted(missing)}"
