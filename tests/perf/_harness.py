"""Helpers for in-process perf tests.

Builds the real FastMCP server (same registration path as mcp_server.main) and drives it through fastmcp's in-memory Client.
Each call exercises the actual dispatch chain: middleware → tool._run → without_injected_parameters
→ thread pool with no HTTP or subprocess.

The stub cluster lets the full KV/SQL++ tool bodies run without Couchbase, isolating framework overhead from I/O.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client, Context, FastMCP
from fastmcp.tools import FunctionTool

from cb_mcp.tool_registration import prepare_tools_for_registration
from cb_mcp.tools import TOOL_ANNOTATIONS
from cb_mcp.utils import AppContext
from cb_mcp.utils.constants import MCP_SERVER_NAME

ITERATIONS = int(os.getenv("CB_MCP_PERF_ITERATIONS", "100"))
ASSERT_ENABLED = os.getenv("CB_MCP_PERF_ASSERT") == "1"

STUB_DOC = {"kind": "perf-stub-doc", "amount": 42, "nested": {"a": 1, "b": "x"}}


# --- Couchbase-free stand-ins -------------------------------------------------


class _StubResult:
    def __init__(self, value: Any) -> None:
        # kv.py reads ``result.content_as[dict]``.
        self.content_as = {dict: value}


class _StubCollection:
    def get(self, document_id: str, *_: Any, **__: Any) -> _StubResult:
        return _StubResult(dict(STUB_DOC, id=document_id))

    def upsert(self, document_id: str, content: Any, *_: Any, **__: Any) -> None:
        return None


class _StubScope:
    def collection(self, name: str) -> _StubCollection:
        return _StubCollection()

    def query(self, statement: str, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return [dict(STUB_DOC)]


class _StubBucket:
    def scope(self, name: str) -> _StubScope:
        return _StubScope()

    def default_collection(self) -> _StubCollection:
        return _StubCollection()


class StubCluster:
    """Just enough of couchbase.cluster.Cluster for the tools under test."""

    def bucket(self, name: str) -> _StubBucket:
        return _StubBucket()

    def close(self) -> None:
        return None


class StubClusterProvider:
    """ClusterProvider that hands out a StubCluster; never touches the network."""

    def __init__(self) -> None:
        self._cluster = StubCluster()

    def get_cluster(self, ctx: Context) -> Any:
        return self._cluster

    def close(self) -> None:
        return None

    def get_configuration(self, ctx: Context) -> Mapping[str, Any]:
        return {"connection_string": "stub://", "username": "stub"}

    def is_connected(self, ctx: Context) -> bool:
        return True


# --- Server construction ------------------------------------------------------


def build_perf_server(provider: Any, *, read_only_mode: bool = False) -> FastMCP:
    """Mirror mcp_server.main's registration, minus logging/telemetry ping.

    Kept in sync by hand with ``src/mcp_server.py``; if registration there
    changes shape, update here too.
    """
    final_tools, confirmation_names, disabled_names = prepare_tools_for_registration(
        read_only_mode=read_only_mode,
        disabled_tools=None,
        confirmation_required_tools=None,
        enforce_scopes=False,
    )
    settings = {
        "connection_string": "stub://",
        "username": "stub",
        "read_only_mode": read_only_mode,
        "transport": "http",
        "disabled_tools": disabled_names,
        "confirmation_required_tools": confirmation_names,
    }

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
        yield AppContext(
            cluster_provider=provider,
            settings=settings,
            read_only_mode=read_only_mode,
        )

    mcp = FastMCP(MCP_SERVER_NAME, lifespan=lifespan)
    for tool in final_tools:
        mcp.add_tool(
            FunctionTool.from_function(
                tool, annotations=TOOL_ANNOTATIONS.get(tool.__name__)
            )
        )
    return mcp


# --- Load driver --------------------------------------------------------------


@dataclass
class LoadResult:
    label: str
    concurrency: int
    latencies_ms: list[float] = field(default_factory=list)
    wall_s: float = 0.0
    errors: int = 0

    def _pct(self, p: float) -> float:
        if not self.latencies_ms:
            return float("nan")
        ordered = sorted(self.latencies_ms)
        return ordered[min(len(ordered) - 1, int(len(ordered) * p))]

    @property
    def p50(self) -> float:
        return self._pct(0.50)

    @property
    def p95(self) -> float:
        return self._pct(0.95)

    @property
    def p99(self) -> float:
        return self._pct(0.99)

    @property
    def mean(self) -> float:
        return (
            statistics.fmean(self.latencies_ms) if self.latencies_ms else float("nan")
        )

    @property
    def ops_s(self) -> float:
        return len(self.latencies_ms) / self.wall_s if self.wall_s else float("nan")


async def run_load(
    mcp: FastMCP,
    tool: str,
    args: Callable[[int], dict[str, Any]] | dict[str, Any],
    *,
    concurrency: int,
    iterations: int = ITERATIONS,
    label: str | None = None,
) -> LoadResult:
    """Run ``concurrency`` workers, each making ``iterations`` calls.

    ``args`` may be a dict or a callable of the worker index (for per-worker
    keys). One in-memory Client is shared, matching many concurrent requests
    on one session.
    """
    result = LoadResult(label=label or tool, concurrency=concurrency)
    build_args = args if callable(args) else (lambda _i: args)

    async with Client(mcp) as client:
        # Warm-up: first call pays one-time costs (schema caches, lazy init).
        await client.call_tool(tool, build_args(0))

        async def worker(idx: int) -> None:
            for _ in range(iterations):
                t0 = time.perf_counter()
                try:
                    await client.call_tool(tool, build_args(idx))
                except Exception:
                    result.errors += 1
                result.latencies_ms.append((time.perf_counter() - t0) * 1000)

        start = time.perf_counter()
        await asyncio.gather(*(worker(i) for i in range(concurrency)))
        result.wall_s = time.perf_counter() - start
    return result


# --- Reporting / assertions ---------------------------------------------------


def format_table(results: list[LoadResult]) -> str:
    header = f"{'label':32s} {'c':>4s} {'n':>6s} {'p50ms':>8s} {'p95ms':>8s} {'p99ms':>8s} {'ops/s':>8s} {'err':>4s}"
    rows = [
        f"{r.label:32s} {r.concurrency:4d} {len(r.latencies_ms):6d} "
        f"{r.p50:8.2f} {r.p95:8.2f} {r.p99:8.2f} {r.ops_s:8.1f} {r.errors:4d}"
        for r in results
    ]
    return "\n".join([header, *rows])


def check_ratio(name: str, observed: float, baseline: float, max_ratio: float) -> None:
    """Assert observed/baseline <= max_ratio, only when CB_MCP_PERF_ASSERT=1.

    Ratios travel across machines far better than absolute milliseconds.
    """
    ratio = observed / baseline if baseline else float("inf")
    msg = f"{name}: {observed:.2f} / {baseline:.2f} = {ratio:.1f}x (max {max_ratio}x)"
    print(msg)
    if ASSERT_ENABLED:
        assert ratio <= max_ratio, msg


def check_ceiling(name: str, observed: float, max_value: float) -> None:
    """Assert observed <= max_value, only when CB_MCP_PERF_ASSERT=1."""
    msg = f"{name}: {observed:.2f} (max {max_value})"
    print(msg)
    if ASSERT_ENABLED:
        assert observed <= max_value, msg
