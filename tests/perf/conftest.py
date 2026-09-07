"""Shared setup for the in-process performance tests.

Everything under tests/perf/ is auto-tagged ``perf`` and skipped unless
``CB_MCP_PERF=1``.

Environment variables:
  - ``CB_MCP_PERF=1``: required to run anything here.
  - ``CB_MCP_PERF_ASSERT=1``: also enforce regression thresholds (default: report only, always pass).
  - ``CB_MCP_PERF_ITERATIONS``: calls per worker (default 200).
  - ``CB_CONNECTION_STRING`` / ``CB_USERNAME`` / ``CB_PASSWORD`` /
    ``CB_MCP_TEST_BUCKET``: only for test_live_cluster.py.
"""

from __future__ import annotations

import os

import pytest

# Keep the telemetry wrapper installed (realistic overhead) but stop the
# reo-census SDK from sending a network event per tool call.
os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("PACKAGE_TRACKER_ANALYTICS", "false")

PERF_ENABLED = os.getenv("CB_MCP_PERF") == "1"


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="perf tests are opt-in: set CB_MCP_PERF=1")
    for item in items:
        if "tests/perf" in str(item.fspath).replace(os.sep, "/"):
            item.add_marker(pytest.mark.perf)
            if not PERF_ENABLED:
                item.add_marker(skip)
