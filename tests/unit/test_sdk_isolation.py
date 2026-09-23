"""A server's process must load only that server's SDK.

``src/mcp_server.py`` imports each server's spec and provider *lazily*,
inside the subcommand body, so that running one server never pays for the
other's SDK. Four separate docstrings state that rule as a fact:
``mcp_server.py``'s module docstring and both of its lazy imports,
``cb_mcp/servers/operational_insights/constants.py``, ``tests/_all_specs.py``,
and CONTRIBUTING.md's "Adding a new MCP server".

Nothing enforced it, and it was silently false: two annotation-only
``from couchbase.cluster import Cluster`` lines (in ``cb_mcp.core.contracts``
and ``cb_mcp.utils.context``) pulled ~120 ``couchbase.*`` modules into the
Operational Insights server's process. Both are now under ``TYPE_CHECKING``.

These tests re-check it the only way that works: in a *clean interpreter*,
because pytest's own process has already imported everything. They are the
reason a future shared module cannot quietly re-introduce a top-level SDK
import.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

#: Repo ``src/``, so the subprocess can import ``cb_mcp`` and ``providers``
#: without depending on how the parent pytest run was invoked.
SRC_DIR = Path(__file__).resolve().parents[2] / "src"

#: Top-level module name of each backing SDK. Distribution names differ
#: (``couchbase`` / ``couchbase-operational-insights``); these are what
#: appear in ``sys.modules``.
COUCHBASE_SDK = "couchbase"
OPERATIONAL_INSIGHTS_SDK = "couchbase_operational_insights"
BOTH_SDKS = (COUCHBASE_SDK, OPERATIONAL_INSIGHTS_SDK)


def _sdks_loaded_by(source: str) -> set[str]:
    """Run ``source`` in a fresh interpreter; return which SDKs it imported.

    A subprocess rather than ``importlib.reload`` or ``sys.modules``
    surgery: the SDKs are already imported in the pytest process (other
    tests need them), and a partially-unloaded package is a worse lie than
    no check at all.
    """
    probe = textwrap.dedent(f"""
        import sys

        {textwrap.indent(source, " " * 8).lstrip()}

        roots = {{name.split(".")[0] for name in sys.modules}}
        print(" ".join(sorted(roots & set({BOTH_SDKS!r}))))
    """)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SRC_DIR), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"probe failed (exit {result.returncode}):\n{result.stderr or result.stdout}"
        )
    return set(result.stdout.split())


@pytest.mark.parametrize(
    "module",
    [
        "cb_mcp.core.contracts",
        "cb_mcp.core.spec",
        "cb_mcp.core.app",
        "cb_mcp.utils.context",
        # The package __init__ re-exports from .context, so importing
        # *anything* from cb_mcp.utils used to drag the Couchbase SDK in.
        "cb_mcp.utils",
        "cb_mcp.tool_registration",
    ],
)
def test_shared_modules_import_no_sdk(module: str) -> None:
    """The server-agnostic layer must be importable without either SDK.

    This is the invariant that makes the per-server lazy imports in
    ``mcp_server.py`` meaningful: if the shared layer pulls an SDK, the
    laziness downstream buys nothing.
    """
    loaded = _sdks_loaded_by(f"import {module}")
    assert not loaded, (
        f"{module} imported {sorted(loaded)} at runtime. Shared modules must "
        "not import a backing SDK; if it is needed only for an annotation, "
        "put it under `if TYPE_CHECKING:` with `from __future__ import "
        "annotations`."
    )


def test_importing_the_cli_loads_no_sdk() -> None:
    """``mcp_server`` itself must stay SDK-free — the lazy rule's whole point.

    It imports *both* servers' constants modules at module scope (for the
    ``transport_options`` / ``logging_options`` factories, which need each
    default port and log file to appear in ``--help``). Those modules are
    SDK-free by design, and each server's ``__init__`` is deliberately inert,
    so neither SDK loads until a subcommand body runs. Nothing else checks
    that: the per-module cases above would all still pass if this file grew a
    top-level ``from cb_mcp.servers.operational.spec import SPEC``.
    """
    loaded = _sdks_loaded_by("import mcp_server")
    assert not loaded, (
        f"importing mcp_server loaded {sorted(loaded)}. Every server's spec and "
        "provider must be imported inside its subcommand body, and the "
        "constants modules imported at module scope must stay SDK-free."
    )


def test_operational_server_does_not_load_the_insights_sdk() -> None:
    """Importing the operational server's spec + provider loads only ``couchbase``."""
    loaded = _sdks_loaded_by(
        "from cb_mcp.servers.operational.spec import SPEC\n"
        "from providers.static import StaticClusterProvider\n"
    )
    assert COUCHBASE_SDK in loaded, (
        "the operational server is expected to load its own SDK eagerly"
    )
    assert OPERATIONAL_INSIGHTS_SDK not in loaded, (
        "the operational server loaded the Operational Insights SDK; some "
        "module it imports has a top-level couchbase_operational_insights import"
    )


def test_insights_server_does_not_load_the_couchbase_sdk() -> None:
    """Importing the OI server's spec + provider loads only the OI SDK.

    The regression this exists for: ``cb_mcp.core.contracts`` and
    ``cb_mcp.utils.context`` annotated ``-> Cluster`` with a runtime import,
    so this assertion failed with ~120 ``couchbase.*`` modules loaded.
    """
    loaded = _sdks_loaded_by(
        "from cb_mcp.servers.operational_insights.spec import SPEC\n"
        "from providers.operational_insights import (\n"
        "    OperationalInsightsClusterProvider,\n"
        ")\n"
    )
    assert OPERATIONAL_INSIGHTS_SDK in loaded, (
        "the Operational Insights server is expected to load its own SDK eagerly"
    )
    assert COUCHBASE_SDK not in loaded, (
        "the Operational Insights server loaded the Couchbase SDK; some "
        "module it imports has a top-level `couchbase` import (check shared "
        "modules under cb_mcp/core and cb_mcp/utils)"
    )
