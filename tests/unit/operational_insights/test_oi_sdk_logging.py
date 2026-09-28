"""Unit tests for the Operational Insights SDK logging bridge.

Covers ``bridge_sdk_logging`` (the ``sdk_log_hook``), using synthetic
loggers so the real ``couchbase_operational_insights`` SDK need not be
imported or connect anywhere.
"""

import io
import logging
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
    tool module, and transitively the SDK itself once a cluster connects)
    must not leave a stray handler on the bare root logger."""
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
