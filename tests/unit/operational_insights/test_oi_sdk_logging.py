"""Unit tests for the Operational Insights SDK logging bridge.

Covers ``bridge_sdk_logging`` (the ``sdk_log_hook``), using synthetic
loggers so the real ``couchbase_operational_insights`` SDK need not be
imported or connect anywhere.
"""

import io
import logging
import os
import subprocess
import sys

from cb_mcp.utils.operational_insights.sdk_logging import bridge_sdk_logging


def test_bridge_routes_sdk_records_into_the_target_root():
    target_root = logging.getLogger("test.oi.target")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    target_root.handlers = [handler]
    target_root.propagate = False

    try:
        bridge_sdk_logging(target_root.name, logging.INFO)
        sdk_logger = logging.getLogger("couchbase_operational_insights")
        sdk_logger.info("hello from sdk")

        assert "hello from sdk" in buf.getvalue()
    finally:
        # Clean up so this test can't leak state into others: the hook
        # mutates the real "couchbase_operational_insights" logger.
        sdk_logger = logging.getLogger("couchbase_operational_insights")
        sdk_logger.handlers = []
        sdk_logger.propagate = True
        target_root.handlers = []
        target_root.propagate = True


def test_bridge_sets_propagate_false_on_the_sdk_logger():
    """Records must not also reach the bare stdlib root via propagation —
    the bridge forwards them explicitly instead."""
    sdk_logger = logging.getLogger("couchbase_operational_insights")
    try:
        bridge_sdk_logging("couchbase", logging.INFO)
        assert sdk_logger.propagate is False
    finally:
        sdk_logger.handlers = []
        sdk_logger.propagate = True


def test_importing_the_oi_spec_adds_no_handler_to_the_stdlib_root():
    """Clean-interpreter check: importing the spec (and therefore every OI
    tool module) at server startup / tool-discovery time — before anything
    ever connects to a cluster — must not leave a stray handler on the bare
    root logger."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging; "
            "import cb_mcp.servers.operational_insights.spec; "
            "assert logging.getLogger().handlers == [], "
            "logging.getLogger().handlers",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_connecting_triggers_no_handler_on_the_stdlib_root():
    """Regression test for the bug ``quiesce_new_root_handlers()`` used to
    clean up after: the SDK's own logging setup
    (``couchbase_operational_insights.common.logging.configure_logging_from_env``)
    runs lazily, the first time ``Cluster.create_instance(...)`` actually
    executes — ``protocol/__init__.py`` (and therefore this side effect) is
    not imported merely by importing the top-level package or this server's
    spec. So the "importing the spec" check above proves nothing about the
    connection-time path; this test drives the real
    ``connect_to_operational_insights_cluster`` call instead, in a
    subprocess, so a regression in the SDK's connect-time logging setup
    would actually be caught.

    ``PYCBOI_LOG_LEVEL`` is set because the *old*, buggy SDK only called
    ``logging.basicConfig()`` on the bare root logger when that env var was
    present (or something had already attached a root handler) — leaving it
    unset would let this test pass against either the old or the fixed SDK,
    proving nothing. The target host is a loopback address nothing listens
    on: the SDK's client construction is lazy (no network I/O happens here),
    so this needs no live cluster and can't hang or flake.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import logging; "
            "from cb_mcp.utils.operational_insights.connection import "
            "connect_to_operational_insights_cluster; "
            "connect_to_operational_insights_cluster('http://127.0.0.1:1', 'u', 'p'); "
            "assert logging.getLogger().handlers == [], "
            "logging.getLogger().handlers",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYCBOI_LOG_LEVEL": "DEBUG"},
    )
    assert result.returncode == 0, result.stderr
