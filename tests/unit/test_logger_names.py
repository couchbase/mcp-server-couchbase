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
import subprocess
import sys

import pytest

import cb_mcp
import cb_mcp.utils.constants as consts
from cb_mcp.servers.operational.constants import (
    FASTMCP_SERVER_NAME,
    OPERATIONAL_LOGGER_NAMESPACE,
)
from cb_mcp.utils.constants import (
    LOGGER_NAMESPACE,
    LOGGER_ROOT,
)
from cb_mcp.utils.logging import configure_logging

# module import path -> the logger name it registers at import time.
# Shared modules sit directly under the package namespace; a server's own
# modules nest one level further under its id. Neither uses the bare
# "couchbase" root, which belongs to the SDK.
EXPECTED_LOGGER_NAMES = {
    # shared
    "cb_mcp.auth": "couchbase.mcp.auth",
    "cb_mcp.core.app": "couchbase.mcp.core.app",
    "cb_mcp.tool_registration": "couchbase.mcp.tool_registration",
    "cb_mcp.utils.cli": "couchbase.mcp.utils.cli",
    "cb_mcp.utils.config": "couchbase.mcp.utils.config",
    "cb_mcp.utils.elicitation": "couchbase.mcp.utils.elicitation",
    "cb_mcp.utils.environment": "couchbase.mcp.utils.environment",
    "cb_mcp.utils.logging": "couchbase.mcp.utils.logging",
    "cb_mcp.utils.scope_enforcement": "couchbase.mcp.utils.scope_enforcement",
    "cb_mcp.utils.telemetry": "couchbase.mcp.utils.telemetry",
    # operational server
    "cb_mcp.tools.operational.collection_management": "couchbase.mcp.operational.tools.collection_management",
    "cb_mcp.tools.operational.index": "couchbase.mcp.operational.tools.index",
    "cb_mcp.tools.operational.kv": "couchbase.mcp.operational.tools.kv",
    "cb_mcp.tools.operational.query": "couchbase.mcp.operational.tools.query",
    "cb_mcp.tools.operational.server": "couchbase.mcp.operational.tools.server",
    "cb_mcp.utils.operational.connection": "couchbase.mcp.operational.utils.connection",
    "cb_mcp.utils.operational.index_utils": "couchbase.mcp.operational.utils.index_utils",
    "providers.static": "couchbase.mcp.operational.providers.static",
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


def test_ambiguous_alias_is_gone():
    """``MCP_SERVER_NAME`` was one name for three jobs and has been removed.

    It is now ``LOGGER_ROOT`` (where handlers attach), ``LOGGER_NAMESPACE``
    (this package's loggers) or a server's ``fastmcp_name`` (wire-visible).
    Reintroducing the alias would re-blur three distinct contracts.
    """
    assert not hasattr(consts, "MCP_SERVER_NAME")


def test_namespaces_nest_correctly():
    """Package namespace under the root; the server's under the package.

    The nesting is what lets handlers attach once at the root while keeping
    each server's records distinguishable in a merged stream.
    """
    assert LOGGER_NAMESPACE.startswith(f"{LOGGER_ROOT}.")
    assert OPERATIONAL_LOGGER_NAMESPACE.startswith(f"{LOGGER_NAMESPACE}.")


def test_nothing_logs_on_the_bare_sdk_root():
    """The bare "couchbase" logger belongs to the Couchbase SDK.

    It creates ``couchbase``, ``couchbase.threshold``, ``couchbase.metrics``
    and ``couchbase.<module>`` loggers of its own — we previously shadowed
    ``couchbase.auth``. Ours must all sit under our own namespace so an SDK
    release can never collide.
    """
    for name in EXPECTED_LOGGER_NAMES.values():
        assert name.startswith(f"{LOGGER_NAMESPACE}."), (
            f"{name!r} is outside {LOGGER_NAMESPACE!r} and risks colliding "
            "with an SDK logger"
        )


def test_fastmcp_name_is_wire_visible():
    """serverInfo.name. Changing it is breaking for every connected client."""
    assert FASTMCP_SERVER_NAME == "couchbase-operational"


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


def test_packages_import_in_any_order():
    """Guard against an import cycle between core and utils.

    ``cb_mcp.core.spec`` importing from ``cb_mcp.utils`` creates a loop:
    ``utils/__init__`` pulls in ``scope_enforcement``, which imports
    ``core.spec``. The test suite hides this — pytest happens to import the
    packages in an order that resolves — so it only surfaces for a caller who
    reaches for ``cb_mcp.core`` first. Each import runs in a clean interpreter.
    """
    for module in (
        "cb_mcp.core",
        "cb_mcp.core.spec",
        "cb_mcp.utils",
        "cb_mcp.tools.operational",
    ):
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"importing {module} first fails:\n{result.stderr}"
        )
