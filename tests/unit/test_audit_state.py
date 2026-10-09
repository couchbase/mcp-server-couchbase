"""Tests for the per-request audit state channel.

The load-bearing test here is
:func:`test_mutation_from_a_worker_thread_is_visible`. FastMCP runs synchronous
tools on a worker thread via ``anyio.to_thread``, which *copies* the context.
Rebinding a contextvar inside the worker would therefore be invisible to the
middleware that installed it. Mutating a shared dict in place is visible,
because both contexts hold a reference to the same object. Every refusal record
depends on that, so it is asserted directly rather than assumed.

Coverage map:
- accessors are no-ops when no state is installed
- install/reset nesting
- first refusal wins
- confirmation and statement-class recording
- extra payload keys survive
- in-place mutation crosses a thread boundary via a copied context
- the correlation id round-trips, and is readable from a worker thread
"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from cb_mcp.audit import state


def test_accessors_are_safe_with_no_state_installed():
    assert state.current() is None
    assert state.get_refusal() is None
    assert state.get_confirmation() is None
    assert state.get_statement_class() is None
    # Recording must not raise when nothing is tracking.
    state.record_refusal(event_id=1, event_name="x", outcome="denied", reason="r")
    state.record_confirmation(state.CONFIRMATION_SKIPPED)
    state.record_statement_class("write")


def test_install_and_reset():
    assert state.current() is None
    token = state.install()
    try:
        # Installed state always carries the correlation slot, empty when the
        # caller supplied none, so a reader never has to distinguish "no state"
        # from "state without a cid".
        assert state.current() == {"cid": None}
    finally:
        state.reset(token)
    assert state.current() is None


def test_correlation_id_round_trips():
    token = state.install(cid="cid-1")
    try:
        assert state.get_cid() == "cid-1"
    finally:
        state.reset(token)
    # The accessor is safe with nothing installed.
    assert state.get_cid() is None


def test_correlation_id_is_readable_from_a_worker_thread():
    """A sync tool runs on a worker thread, and needs the request's cid there.

    This is what a later phase depends on to propagate the ``cid`` to Couchbase
    Server as a SQL++ ``client_context_id`` from inside tool code.
    """
    token = state.install(cid="cid-worker")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            # copy_context() mirrors what anyio.to_thread does for sync tools.
            ctx = copy_context()
            seen = pool.submit(ctx.run, state.get_cid)
            assert seen.result() == "cid-worker"
    finally:
        state.reset(token)


def test_refusal_round_trip_with_extra_payload():
    token = state.install()
    try:
        state.record_refusal(
            event_id=57377,
            event_name="scope check denied",
            outcome="denied",
            reason="missing_scope",
            required_scope=["couchbase-mcp:write"],
        )
        refusal = state.get_refusal()
        assert refusal == {
            "event_id": 57377,
            "event_name": "scope check denied",
            "outcome": "denied",
            "reason": "missing_scope",
            "required_scope": ["couchbase-mcp:write"],
        }
    finally:
        state.reset(token)


def test_first_refusal_wins():
    """The earliest gate to fire is the one that stopped the request."""
    token = state.install()
    try:
        state.record_refusal(
            event_id=57377,
            event_name="scope check denied",
            outcome="denied",
            reason="missing_scope",
        )
        state.record_refusal(
            event_id=57490,
            event_name="confirmation declined",
            outcome="blocked",
            reason="confirmation_declined",
        )
        assert state.get_refusal()["event_id"] == 57377
    finally:
        state.reset(token)


def test_confirmation_and_statement_class():
    token = state.install()
    try:
        state.record_confirmation(state.CONFIRMATION_ACCEPTED)
        state.record_statement_class("write")
        assert state.get_confirmation() == "accepted"
        assert state.get_statement_class() == "write"
        # Last write wins for these, unlike refusals.
        state.record_confirmation(state.CONFIRMATION_DECLINED)
        assert state.get_confirmation() == "declined"
    finally:
        state.reset(token)


def test_mutation_from_a_worker_thread_is_visible():
    """In-place mutation crosses a copied context; a rebind would not.

    This mirrors exactly what ``anyio.to_thread.run_sync`` does for a
    synchronous MCP tool: copy the current context and run the callable inside
    it on another thread.
    """
    token = state.install()
    try:
        context = contextvars.copy_context()

        def in_worker() -> None:
            state.record_refusal(
                event_id=57488,
                event_name="write blocked (read-only mode)",
                outcome="blocked",
                reason="read_only_mode",
            )
            state.record_statement_class("write")

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(context.run, in_worker).result()

        # Read back from the *installing* context, not the worker's copy.
        refusal = state.get_refusal()
        assert refusal is not None
        assert refusal["event_id"] == 57488
        assert state.get_statement_class() == "write"
    finally:
        state.reset(token)


def test_rebinding_in_a_worker_would_not_be_visible():
    """Documents why the shared-dict design is necessary, not incidental."""
    token = state.install()
    try:
        context = contextvars.copy_context()

        def rebind_in_worker() -> None:
            state.install()  # rebinds the contextvar inside the copy
            state.record_confirmation(state.CONFIRMATION_SKIPPED)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(context.run, rebind_in_worker).result()

        # The installing context never sees it — hence record_* must mutate.
        assert state.get_confirmation() is None
    finally:
        state.reset(token)
