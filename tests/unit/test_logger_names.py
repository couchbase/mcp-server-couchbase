"""Snapshot of the logger names every module registers.

These strings are operator-facing: they appear as ``%(name)s`` in every log
line, they are what support runbooks tell customers to grep for, and they are
the hierarchy that ``configure_logging`` attaches its per-level rotating
handlers to (a single attach point at the root, see
``cb_mcp.utils.logging.configure_logging``).

The refactor that splits ``MCP_SERVER_NAME`` into a logging root and a
wire-visible FastMCP name touches every one of these call sites. This snapshot
exists so that split cannot silently rename one: any change here must be a
deliberate edit to the expected mapping below, not a side effect.

Every logger must also stay *under* the root, or ``configure_logging`` will
attach handlers that never see its records.
"""

import importlib
import logging
import pathlib

import pytest

import cb_mcp
from cb_mcp.utils.constants import (
    FASTMCP_SERVER_NAME,
    LOGGER_ROOT,
    MCP_SERVER_NAME,
)
from cb_mcp.utils.logging import configure_logging

# module import path -> the logger name it registers at import time.
EXPECTED_LOGGER_NAMES = {
    "cb_mcp.auth": "couchbase.auth",
    "cb_mcp.core.app": "couchbase.core.app",
    "cb_mcp.tool_registration": "couchbase.tool_registration",
    "cb_mcp.tools.collection_management": "couchbase.tools.collection_management",
    "cb_mcp.tools.index": "couchbase.tools.index",
    "cb_mcp.tools.kv": "couchbase.tools.kv",
    "cb_mcp.tools.query": "couchbase.tools.query",
    "cb_mcp.tools.server": "couchbase.tools.server",
    "cb_mcp.utils.cli": "couchbase.utils.cli",
    "cb_mcp.utils.config": "couchbase.utils.config",
    "cb_mcp.utils.connection": "couchbase.utils.connection",
    "cb_mcp.utils.elicitation": "couchbase.utils.elicitation",
    "cb_mcp.utils.environment": "couchbase.utils.environment",
    "cb_mcp.utils.index_utils": "couchbase.utils.index_utils",
    "cb_mcp.utils.scope_enforcement": "couchbase.utils.scope_enforcement",
    "cb_mcp.utils.telemetry": "couchbase.utils.telemetry",
    "providers.static": "couchbase.providers.static",
}

# The root the handlers attach to. Everything above must be a descendant.
EXPECTED_LOGGER_ROOT = "couchbase"


@pytest.mark.parametrize(
    ("module_path", "expected_name"), sorted(EXPECTED_LOGGER_NAMES.items())
)
def test_module_logger_name_is_stable(module_path, expected_name):
    """Each module registers its logger under the documented name."""
    module = importlib.import_module(module_path)
    assert module.logger.name == expected_name


def test_every_logger_is_under_the_root():
    """A logger outside the root would never receive the configured handlers."""
    for module_path, name in EXPECTED_LOGGER_NAMES.items():
        assert name == EXPECTED_LOGGER_ROOT or name.startswith(
            f"{EXPECTED_LOGGER_ROOT}."
        ), (
            f"{module_path} registers {name!r}, which is outside {EXPECTED_LOGGER_ROOT!r}"
        )


def test_logger_root_matches_constant():
    """The snapshot root tracks whatever constant the modules derive from."""
    assert LOGGER_ROOT == EXPECTED_LOGGER_ROOT


def test_deprecated_alias_still_resolves_to_the_logging_root():
    """``MCP_SERVER_NAME`` is retained for external importers.

    It aliases the *logging* root specifically, not the wire-visible FastMCP
    name — the two happen to share a value today, and this pins which of the
    two the alias follows if they ever diverge.
    """
    assert MCP_SERVER_NAME == LOGGER_ROOT


def test_fastmcp_name_is_wire_visible_and_unchanged():
    """Changing this breaks every connected client, independent of logging."""
    assert FASTMCP_SERVER_NAME == "couchbase"


def test_configure_logging_attaches_to_the_root():
    """``configure_logging`` must target the same root the modules live under."""
    root = logging.getLogger(EXPECTED_LOGGER_ROOT)
    before = list(root.handlers)
    try:
        configure_logging(
            level="INFO",
            sinks={"stderr"},
            log_file="mcp_server.log",
            log_backup_count=1,
        )
        assert root.handlers, (
            f"configure_logging attached no handlers to {EXPECTED_LOGGER_ROOT!r}"
        )
    finally:
        root.handlers = before


def test_snapshot_covers_every_module_logger():
    """Guard against a new module registering a logger nobody snapshotted.

    Walks the shipped source tree for ``getLogger`` call sites so that adding a
    module with a logger forces an explicit update here.
    """
    src_root = pathlib.Path(cb_mcp.__file__).parent.parent
    found: set[str] = set()
    for path in src_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        # Module-level assignment of the form ``logger = ...getLogger(...)``.
        if "\nlogger = " not in text and not text.startswith("logger = "):
            continue
        rel = path.relative_to(src_root).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        found.add(".".join(parts))

    # mcp_server is the CLI host; it logs at the bare root, not a child.
    found.discard("mcp_server")
    assert found == set(EXPECTED_LOGGER_NAMES), (
        "Logger snapshot is out of date. Added: "
        f"{sorted(found - set(EXPECTED_LOGGER_NAMES))}, "
        f"removed: {sorted(set(EXPECTED_LOGGER_NAMES) - found)}"
    )
