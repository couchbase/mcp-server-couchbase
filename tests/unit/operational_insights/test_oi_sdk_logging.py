"""Unit tests for the Operational Insights SDK logging bridge.

Covers ``bridge_sdk_logging`` (the ``sdk_log_hook``) and
``quiesce_new_root_handlers`` in isolation, using synthetic loggers so the
real ``couchbase_operational_insights`` SDK need not be imported or connect
anywhere.
"""

import io
import logging
import subprocess
import sys

from cb_mcp.utils.operational_insights.sdk_logging import (
    bridge_sdk_logging,
    quiesce_new_root_handlers,
)


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


def test_quiesce_removes_a_handler_added_inside_the_block():
    root = logging.getLogger()
    pre_existing = logging.StreamHandler()
    root.addHandler(pre_existing)
    try:
        with quiesce_new_root_handlers():
            # Simulate the SDK's own connect-time side effect: a handler
            # added by the code running inside the block.
            stray = logging.StreamHandler()
            root.addHandler(stray)

        assert stray not in root.handlers
        # Added before the block started, so it must survive.
        assert pre_existing in root.handlers
    finally:
        root.removeHandler(pre_existing)
        pre_existing.close()


def test_quiesce_removes_the_handler_even_when_the_block_raises():
    root = logging.getLogger()
    stray = logging.StreamHandler()

    try:
        with quiesce_new_root_handlers():
            root.addHandler(stray)
            raise ValueError("connection failed")
    except ValueError:
        pass

    assert stray not in root.handlers


def test_quiesce_does_not_remove_a_handler_added_before_the_block():
    """The regression this guards: a handler added *after* this module was
    imported, but *before* the connect-time block runs (e.g. the host's own
    ``logging.basicConfig()`` call), must never be mistaken for the SDK's
    and removed. Only what changes during the block itself counts."""
    root = logging.getLogger()
    host_handler = logging.StreamHandler()
    root.addHandler(host_handler)  # added well after this module was imported
    try:
        with quiesce_new_root_handlers():
            pass  # nothing added during the block this time

        assert host_handler in root.handlers
    finally:
        root.removeHandler(host_handler)
        host_handler.close()


def test_importing_the_oi_spec_adds_no_handler_to_the_stdlib_root():
    """Clean-interpreter check: importing the spec (and therefore every OI
    tool module) must not leave a stray handler on the bare root logger."""
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
