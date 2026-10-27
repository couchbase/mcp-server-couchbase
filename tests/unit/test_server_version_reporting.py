"""DA-2343: a server reports its own build, not FastMCP's.

``build_app`` constructed ``FastMCP`` without ``version=``, so the handshake
reported the framework version — it moved between runs of an identical build
(3.4.8 -> 3.4.9 -> 3.4.11 under couchbase-mcp-server 1.0.2rc5) while the build
stayed put, leaving nothing on the protocol to identify the server.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from importlib.metadata import version as dist_version
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from _all_specs import ALL_SPECS
from fastmcp import Context

from cb_mcp.core.app import build_app
from cb_mcp.tools.status import get_server_configuration_status
from cb_mcp.version import server_version


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.id)
def test_handshake_reports_the_build_not_the_framework(spec):
    """``serverInfo.version`` is this package's version, not fastmcp's."""
    mcp = build_app(
        spec,
        tools=[],
        settings={"transport": "stdio"},
        provider_factory=MagicMock,
    )

    assert mcp.version == server_version()
    # The regression itself: the two are different numbers.
    assert mcp.version != dist_version("fastmcp")


def test_status_tool_reports_the_version():
    """Readable from a tool result, not only the initialize frame."""
    ctx = cast(
        Context,
        SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context=SimpleNamespace(
                    cluster_provider=None,
                    settings={},
                    logging_config=None,
                    audit_config=None,
                )
            )
        ),
    )

    assert get_server_configuration_status(ctx)["version"] == server_version()


def test_perf_harness_app_reports_the_build():
    """The harness builds its own app; it must not regress to fastmcp's version.

    This is the consumer named in the ticket — a perf run that cannot identify
    the build it measured is the reason the bug was filed. ``tests/`` is not a
    package, so the module is loaded by path.
    """
    harness_path = pathlib.Path(__file__).parents[1] / "perf" / "_harness.py"
    spec = importlib.util.spec_from_file_location("_perf_harness", harness_path)
    assert spec is not None and spec.loader is not None
    harness = importlib.util.module_from_spec(spec)
    # Registered before exec: the module defines dataclasses, whose annotation
    # resolution looks the module up in sys.modules.
    sys.modules[spec.name] = harness
    try:
        spec.loader.exec_module(harness)
        mcp = harness.build_perf_server(MagicMock())
    finally:
        sys.modules.pop(spec.name, None)

    assert mcp.version == server_version()
