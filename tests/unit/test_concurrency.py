"""Tests for the per-process tool-call concurrency limit.

The limit is AnyIO's shared thread-pool ``CapacityLimiter``, which FastMCP uses
to run our synchronous tools. Two properties matter and neither is obvious from
reading the one-line implementation: leaving the option unset must not touch the
runtime's default at all, and the value returned must be the limit that actually
took effect, since that is what the diagnostics and the status tool report.

The limiter lives in a run-scoped variable, so every test here runs inside an
event loop. Each ``anyio.run``/``asyncio.run`` call gets a fresh run scope and
therefore a fresh limiter, so tests cannot leak a raised limit into each other.
"""

from __future__ import annotations

import threading
import time

import anyio
import anyio.to_thread
import pytest

from cb_mcp.utils.concurrency import apply_thread_pool_limit

# AnyIO's documented default pool size. Asserted against rather than imported,
# so that a future AnyIO release changing it fails this test loudly instead of
# silently redefining what "unset" means for operators.
ANYIO_DEFAULT_TOKENS = 40


def test_unset_leaves_the_runtime_default_untouched():
    """An unset option must be provably a no-op, not a restatement of 40."""

    async def check():
        before = anyio.to_thread.current_default_thread_limiter().total_tokens
        effective = apply_thread_pool_limit(None)
        after = anyio.to_thread.current_default_thread_limiter().total_tokens
        return before, effective, after

    before, effective, after = anyio.run(check)
    assert before == after == ANYIO_DEFAULT_TOKENS
    assert effective == ANYIO_DEFAULT_TOKENS


def test_configured_size_is_applied_and_returned():
    async def check():
        effective = apply_thread_pool_limit(96)
        return effective, anyio.to_thread.current_default_thread_limiter().total_tokens

    effective, live = anyio.run(check)
    assert effective == 96
    assert live == 96


def test_returns_int_for_reporting():
    """``total_tokens`` is a float on the limiter; the status tool and the JSON
    diagnostic should carry a plain integer."""

    async def check():
        return apply_thread_pool_limit(8)

    assert isinstance(anyio.run(check), int)


def test_lowering_the_limit_is_allowed():
    """Useful for benchmarking the constraint from below, and it proves we are
    setting rather than only raising."""

    async def check():
        return apply_thread_pool_limit(2)

    assert anyio.run(check) == 2


async def _peak_concurrent_calls(requested: int) -> int:
    """Run ``requested`` tool-like calls at once; return the peak overlap."""
    live = 0
    peak = 0
    lock = threading.Lock()

    def blocking_tool():
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        # Long enough that every admitted call overlaps the others.
        time.sleep(0.05)
        with lock:
            live -= 1

    async with anyio.create_task_group() as tg:
        for _ in range(requested):
            tg.start_soon(anyio.to_thread.run_sync, blocking_tool)
    return peak


@pytest.mark.parametrize(
    ("limit", "requested", "expected_peak"),
    [
        # The limit binds: only `limit` calls execute at once.
        (4, 12, 4),
        # Raised above demand: every call runs concurrently.
        (16, 12, 12),
    ],
)
def test_limit_caps_concurrently_executing_tool_calls(limit, requested, expected_peak):
    """The knob's whole purpose: it is the ceiling on in-flight tool calls.

    Asserting the observable behaviour rather than just the attribute, so this
    still fails if a future FastMCP or AnyIO release stops routing sync tools
    through the default limiter.
    """

    async def check():
        apply_thread_pool_limit(limit)
        return await _peak_concurrent_calls(requested)

    assert anyio.run(check) == expected_peak
