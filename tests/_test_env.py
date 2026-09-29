"""Shared environment helpers for the MCP server test suite.

These helpers are imported by ``tests/integration/conftest.py`` and
``tests/accuracy/conftest.py``. Keeping them in their own module avoids the
``conftest``-vs-``conftest`` name collision that would otherwise force a
nested conftest to load the parent via ``importlib``.
"""

from __future__ import annotations

import os

import pytest

REQUIRED_ENV_VARS = ("CB_CONNECTION_STRING", "CB_USERNAME", "CB_PASSWORD")

#: Operational Insights equivalents. Kept as a separate tuple, not folded
#: into REQUIRED_ENV_VARS: a developer with only a Couchbase Server cluster
#: must still be able to run the operational integration tier without every
#: OI test erroring for missing credentials it never needed.
OI_REQUIRED_ENV_VARS = (
    "CB_OI_CONNECTION_STRING",
    "CB_OI_USERNAME",
    "CB_OI_PASSWORD",
)


def _build_env() -> dict[str, str]:
    """Build the environment passed to the test server process."""
    env = os.environ.copy()
    missing = [var for var in REQUIRED_ENV_VARS if not env.get(var)]
    if missing:
        pytest.skip(
            "Integration tests require demo cluster credentials. "
            f"Missing env vars: {', '.join(missing)}"
        )

    env["CB_MCP_TRANSPORT"] = "stdio"
    env["CB_MCP_READ_ONLY_MODE"] = "false"
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def get_test_bucket() -> str | None:
    """Get the test bucket name from environment, or None if not set."""
    return os.getenv("CB_MCP_TEST_BUCKET")


def get_test_scope() -> str:
    """Get the test scope name from environment, defaults to _default."""
    return os.getenv("CB_MCP_TEST_SCOPE", "_default")


def get_test_collection() -> str:
    """Get the test collection name from environment, defaults to _default."""
    return os.getenv("CB_MCP_TEST_COLLECTION", "_default")


def require_test_bucket() -> str:
    """Get the test bucket name, skipping test if not set."""
    bucket = get_test_bucket()
    if not bucket:
        pytest.skip("CB_MCP_TEST_BUCKET not set")
    return bucket


def oi_env_available() -> bool:
    """True if every Operational Insights credential is set.

    A predicate rather than a skip, so a directory-level conftest can apply
    the skip at collection time instead of once per test (matching
    ``tests/perf/conftest.py``'s ``PERF_ENABLED`` pattern).
    """
    return all(os.environ.get(var) for var in OI_REQUIRED_ENV_VARS)


def build_oi_env() -> dict[str, str]:
    """Build the environment passed to an Operational Insights test process.

    Skips (rather than errors) if credentials are missing — same policy as
    ``_build_env()``.
    """
    env = os.environ.copy()
    if not oi_env_available():
        pytest.skip(
            "Operational Insights integration tests require a live cluster. "
            f"Missing env vars: {', '.join(OI_REQUIRED_ENV_VARS)}"
        )
    env["CB_MCP_TRANSPORT"] = "stdio"
    env["CB_MCP_READ_ONLY_MODE"] = "false"
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def get_oi_test_database() -> str:
    """Get the OI test database name from environment, defaults to Default."""
    return os.getenv("CB_OI_TEST_DATABASE", "Default")


def get_oi_test_scope() -> str:
    """Get the OI test scope name from environment, defaults to Default."""
    return os.getenv("CB_OI_TEST_SCOPE", "Default")


def get_oi_test_collection() -> str | None:
    """Get the OI test collection name from environment, or None if not set."""
    return os.getenv("CB_OI_TEST_COLLECTION")
