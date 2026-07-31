"""
Shared fixtures and utilities for MCP server integration tests.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _test_env import (
    REQUIRED_ENV_VARS,
    _build_env,
    get_test_bucket,
    get_test_collection,
    get_test_scope,
    require_test_bucket,
)
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

if TYPE_CHECKING:
    from typing import TextIO

__all__ = [
    "EXPECTED_TOOLS",
    "TOOLS_BY_CATEGORY",
    "TOOL_REQUIRED_PARAMS",
    "_build_env",
    "create_logging_test_session",
    "create_mcp_session",
    "ensure_list",
    "extract_payload",
    "get_test_bucket",
    "get_test_collection",
    "get_test_scope",
    "require_test_bucket",
]
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Tools we expect to be registered by the server
EXPECTED_TOOLS = {
    "get_buckets_in_cluster",
    "get_server_configuration_status",
    "test_cluster_connection",
    "get_scopes_and_collections_in_bucket",
    "get_collections_in_scope",
    "get_scopes_in_bucket",
    "get_document_by_id",
    "upsert_document_by_id",
    "insert_document_by_id",
    "replace_document_by_id",
    "delete_document_by_id",
    "get_schema_for_collection",
    "run_sql_plus_plus_query",
    "explain_sql_plus_plus_query",
    "get_index_advisor_recommendations",
    "list_indexes",
    "get_cluster_health_and_services",
    # Performance analysis tools
    "get_longest_running_queries",
    "get_most_frequent_queries",
    "get_queries_with_largest_response_sizes",
    "get_queries_with_large_result_count",
    "get_queries_using_primary_index",
    "get_queries_not_using_covering_index",
    "get_queries_not_selective",
}

# Tools organized by category for validation
TOOLS_BY_CATEGORY = {
    "server": {
        "get_server_configuration_status",
        "test_cluster_connection",
        "get_buckets_in_cluster",
        "get_scopes_in_bucket",
        "get_scopes_and_collections_in_bucket",
        "get_collections_in_scope",
        "get_cluster_health_and_services",
    },
    "kv": {
        "get_document_by_id",
        "upsert_document_by_id",
        "insert_document_by_id",
        "replace_document_by_id",
        "delete_document_by_id",
    },
    "query": {
        "get_schema_for_collection",
        "run_sql_plus_plus_query",
        "explain_sql_plus_plus_query",
    },
    "index": {
        "list_indexes",
        "get_index_advisor_recommendations",
    },
    "performance": {
        "get_longest_running_queries",
        "get_most_frequent_queries",
        "get_queries_with_largest_response_sizes",
        "get_queries_with_large_result_count",
        "get_queries_using_primary_index",
        "get_queries_not_using_covering_index",
        "get_queries_not_selective",
    },
}

# Expected required parameters for tools that need them
TOOL_REQUIRED_PARAMS = {
    "get_scopes_in_bucket": ["bucket_name"],
    "get_scopes_and_collections_in_bucket": ["bucket_name"],
    "get_collections_in_scope": ["bucket_name", "scope_name"],
    "get_document_by_id": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "document_id",
    ],
    "upsert_document_by_id": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "document_id",
        "document_content",
    ],
    "delete_document_by_id": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "document_id",
    ],
    "insert_document_by_id": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "document_id",
        "document_content",
    ],
    "replace_document_by_id": [
        "bucket_name",
        "scope_name",
        "collection_name",
        "document_id",
        "document_content",
    ],
    "get_schema_for_collection": ["bucket_name", "scope_name", "collection_name"],
    "run_sql_plus_plus_query": ["bucket_name", "scope_name", "query"],
    "explain_sql_plus_plus_query": ["bucket_name", "scope_name", "query"],
    "get_index_advisor_recommendations": ["bucket_name", "scope_name", "query"],
}

# Default timeout (seconds) to guard against hangs when the Couchbase cluster
# is unreachable or slow. Override with CB_MCP_TEST_TIMEOUT if needed.
DEFAULT_TIMEOUT = int(os.getenv("CB_MCP_TEST_TIMEOUT", "120"))


def _build_stdio_subprocess_env() -> dict[str, str]:
    """Build the environment for a freshly-spawned ``mcp_server`` subprocess.

    Only used on the stdio path. For http/sse the MCP server is started
    outside pytest by the CI workflow, so its env is whatever CI passed
    at startup — the test runner doesn't get to override it per-test.
    """
    env = os.environ.copy()
    missing = [var for var in REQUIRED_ENV_VARS if not env.get(var)]
    if missing:
        pytest.skip(
            "Integration tests require demo cluster credentials. "
            f"Missing env vars: {', '.join(missing)}"
        )

    # The spawned subprocess must use stdio for stdio_client to talk to it,
    # regardless of what CB_MCP_TRANSPORT was set to in the outer process.
    env["CB_MCP_TRANSPORT"] = "stdio"
    # Disable read-only mode for integration tests so all tools are available
    # This allows testing of KV write tools (upsert, insert, replace, delete)
    env["CB_MCP_READ_ONLY_MODE"] = "false"
    # Ensure unbuffered output to avoid stdout/stderr buffering surprises
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Forward coverage subprocess config so the child mcp_server process is
    # instrumented when pytest is run with --cov. Requires a .pth file in
    # site-packages that calls coverage.process_startup().
    coverage_rc = PROJECT_ROOT / ".coveragerc"
    if coverage_rc.exists():
        env.setdefault("COVERAGE_PROCESS_START", str(coverage_rc))
    return env


@asynccontextmanager
async def _stdio_session(
    extra_env: dict[str, str] | None,
) -> AsyncIterator[ClientSession]:
    """Spawn a fresh ``mcp_server`` subprocess and yield a session to it."""
    env = _build_stdio_subprocess_env()
    if extra_env:
        env.update(extra_env)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        env=env,
    )
    async with asyncio.timeout(DEFAULT_TIMEOUT):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def _streamable_http_session() -> AsyncIterator[ClientSession]:
    """Connect to an already-running MCP server via streamable HTTP.

    The server itself is launched by the CI workflow before pytest starts
    (see [test.yml] HTTP server-startup step). ``MCP_SERVER_URL`` must
    point at the streamable-http endpoint (typically ``/mcp``).
    """
    url = os.getenv("MCP_SERVER_URL")
    if not url:
        pytest.skip(
            "MCP_SERVER_URL must be set when CB_MCP_TRANSPORT=http. "
            "CI sets this to e.g. http://127.0.0.1:8000/mcp after starting the server."
        )
    async with asyncio.timeout(DEFAULT_TIMEOUT):
        async with streamablehttp_client(url) as (
            read_stream,
            write_stream,
            _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def _sse_session() -> AsyncIterator[ClientSession]:
    """Connect to an already-running MCP server via Server-Sent Events.

    The server itself is launched by the CI workflow before pytest starts.
    ``MCP_SERVER_URL`` must point at the SSE endpoint (typically ``/sse``).
    """
    url = os.getenv("MCP_SERVER_URL")
    if not url:
        pytest.skip(
            "MCP_SERVER_URL must be set when CB_MCP_TRANSPORT=sse. "
            "CI sets this to e.g. http://127.0.0.1:8000/sse after starting the server."
        )
    async with asyncio.timeout(DEFAULT_TIMEOUT):
        async with sse_client(url) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def create_mcp_session(
    extra_env: dict[str, str] | None = None,
) -> AsyncIterator[ClientSession]:
    """Create a fresh MCP client session using the configured transport.

    Transport selection is driven by the ``CB_MCP_TRANSPORT`` env var
    (default ``stdio``):

    - ``stdio``: spawn a fresh ``mcp_server`` subprocess per test.
    - ``http`` / ``streamable-http``: connect to an already-running server
      at ``MCP_SERVER_URL`` via the streamable-HTTP client.
    - ``sse``: connect to an already-running server at ``MCP_SERVER_URL``
      via the SSE client.

    Args:
        extra_env: Optional mapping of env vars to merge on top of the
            default test env. When provided, the session is ALWAYS created
            via stdio regardless of ``CB_MCP_TRANSPORT`` — the http/sse
            transports use a single long-lived server started outside
            pytest, so we can't reconfigure it per-test. Tests that pass
            ``extra_env`` (e.g., read-only-mode tests) therefore implicitly
            run on stdio even in HTTP/SSE CI jobs. This is an intentional
            silent fallback so those tests still get exercised everywhere
            instead of being skipped half the time.
    """
    # Passing extra_env requires control of the server lifecycle — only
    # the stdio path spawns a subprocess we can reconfigure.
    if extra_env is not None:
        async with _stdio_session(extra_env) as session:
            yield session
        return

    transport = os.getenv("CB_MCP_TRANSPORT", "stdio").lower()

    if transport == "stdio":
        ctx_mgr = _stdio_session(None)
    elif transport in ("http", "streamable-http"):
        ctx_mgr = _streamable_http_session()
    elif transport == "sse":
        ctx_mgr = _sse_session()
    else:
        raise ValueError(
            f"Unsupported CB_MCP_TRANSPORT={transport!r}. "
            "Expected one of: stdio, http, streamable-http, sse."
        )

    async with ctx_mgr as session:
        yield session


def is_error_response(response: Any) -> bool:
    """Return True if the MCP tool response represents an error.

    Different MCP client versions expose this flag as ``isError`` or
    ``is_error``; this helper normalizes the two so tests don't have to.
    """
    return bool(
        getattr(response, "isError", None) or getattr(response, "is_error", False)
    )


def extract_payload(response: Any) -> Any:
    """Extract a usable payload from a tool response.

    MCP tool responses can return data in different formats:
    - A single content block with JSON-encoded data (dict, list, etc.)
    - Multiple content blocks, one per list item (for list returns)

    This function handles both cases.
    """
    content = getattr(response, "content", None) or []
    if not content:
        return None

    # If there are multiple content blocks, collect them all as a list
    # (each item in a list return may be a separate content block)
    if len(content) > 1:
        items = []
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                try:
                    items.append(json.loads(text))
                except json.JSONDecodeError:
                    items.append(text)
        return items if items else None

    # Single content block - try to parse as JSON
    first = content[0]
    raw = getattr(first, "text", None)
    if raw is None and hasattr(first, "data"):
        raw = first.data

    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    return raw


def ensure_list(value: Any) -> list[Any]:
    """Ensure the value is a list.

    MCP can return single-item lists as just the item (not wrapped in a list).
    This helper wraps single non-list values in a list for consistent handling.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


@asynccontextmanager
async def create_logging_test_session(
    extra_args: list[str] | None = None,
    env_overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
    stderr_buffer: TextIO | None = None,
) -> AsyncIterator[ClientSession]:
    """Spawn the MCP server for CLI / logging tests; no cluster credentials.

    Cluster credentials are deliberately stripped from the inherited environment
    so the server boots in "no cluster" lazy mode (which is fine — tools that
    don't touch the cluster, like ``get_server_configuration_status``, work
    without connectivity). Use this helper for tests that exercise CLI flags,
    env-var routing, or filesystem effects of logging — not for tests that
    need to call cluster-touching tools.

    Optional arguments:
      - ``extra_args``: extra CLI flags appended after ``python -m mcp_server``.
      - ``env_overrides``: merged onto the server's environment after credential
        stripping. Use to set ``CB_MCP_LOG_LEVEL`` and friends.
      - ``cwd``: working directory for the spawned process. Set to a
        ``tmp_path`` when verifying default CWD-relative file paths.
      - ``stderr_buffer``: a writable file object backed by a real file
        descriptor (e.g. ``tmp_path / "server.stderr"`` opened in ``"w"``
        mode). The MCP SDK passes this straight to ``asyncio.subprocess``,
        which requires ``.fileno()`` — ``io.StringIO`` will not work.
        Read the captured stderr back from the same path after the session
        closes.
    """
    env = os.environ.copy()
    # Strip credentials so the server starts in lazy mode without skipping.
    for var in REQUIRED_ENV_VARS:
        env.pop(var, None)
    env["PYTHONUNBUFFERED"] = "1"
    # Match _build_env(): always spawn the subprocess in stdio mode so the
    # same test suite runs unchanged under the http-transport CI job (which
    # exports CB_MCP_TRANSPORT=http and keeps a standing server on :8000).
    env["CB_MCP_TRANSPORT"] = "stdio"
    env.pop("MCP_TRANSPORT", None)
    if env_overrides:
        env.update(env_overrides)

    # Ensure the subprocess imports the current source, not a stale
    # site-packages install that may lack new CLI flags.
    src_path = str(PROJECT_ROOT.parent / "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    if existing_pythonpath:
        env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing_pythonpath}"
    else:
        env["PYTHONPATH"] = src_path

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server", *(extra_args or [])],
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )

    client_kwargs: dict[str, Any] = {}
    if stderr_buffer is not None:
        client_kwargs["errlog"] = stderr_buffer

    async with asyncio.timeout(DEFAULT_TIMEOUT):
        async with stdio_client(params, **client_kwargs) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session
