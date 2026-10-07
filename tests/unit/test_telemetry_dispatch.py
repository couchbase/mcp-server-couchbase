"""Tests for the telemetry event dispatcher.

The dispatcher exists so that telemetry cannot cost the server throughput or
memory no matter how the collector behaves, and so that it never raises into a
tool call. These tests therefore concentrate on the failure modes: a full
queue, a sender that always fails, a sender that raises, and sampling. The
happy path is covered here too, because everything else is meaningless if
events are not delivered at all.

Each test builds its own dispatcher with explicit arguments rather than
relying on the env vars, except the two that exist to check the env vars.
"""

from __future__ import annotations

import queue
import threading
import time

import pytest

from cb_mcp.utils import telemetry_dispatch
from cb_mcp.utils.telemetry_dispatch import EventDispatcher, dispatch_enabled


class _Sender:
    """Records what it was asked to send and answers however the test wants."""

    def __init__(self, result=True):
        self.sent: list[dict] = []
        self.result = result
        self._lock = threading.Lock()
        self.calls = threading.Event()

    def __call__(self, event: dict) -> bool:
        with self._lock:
            self.sent.append(event)
        self.calls.set()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def dispatchers():
    """Close every dispatcher a test builds, so senders don't outlive it."""
    built: list[EventDispatcher] = []

    def build(*args, **kwargs) -> EventDispatcher:
        d = EventDispatcher(*args, **kwargs)
        built.append(d)
        return d

    yield build
    for d in built:
        d.close()


class TestDelivery:
    def test_events_reach_the_sender_unchanged(self, dispatchers):
        sender = _Sender()
        d = dispatchers(sender, senders=1)

        event = {"activity_type": "tool_call", "tool_name": "get_document_by_id"}
        assert d.submit(event) is True
        assert d.flush() is True

        assert sender.sent == [event]
        assert d.stats["enqueued"] == 1
        assert d.stats["delivered"] == 1

    def test_submit_does_not_block_on_a_slow_sender(self, dispatchers):
        """The request path must not wait for the collector."""
        release = threading.Event()

        def slow(event):
            release.wait(5)
            return True

        d = dispatchers(slow, senders=1, queue_max=10)
        started = time.monotonic()
        for i in range(10):
            d.submit({"n": i})
        elapsed = time.monotonic() - started
        release.set()
        assert elapsed < 1.0

    def test_delivery_failure_is_counted_not_raised(self, dispatchers):
        sender = _Sender(result=False)
        d = dispatchers(sender, senders=1)
        d.submit({"n": 1})
        assert d.flush() is True
        _wait_for(lambda: d.stats["failed"] >= 1)
        assert d.stats["delivered"] == 0

    def test_a_raising_sender_never_escapes(self, dispatchers):
        """A collector client that throws must not kill the sender thread."""
        sender = _Sender(result=RuntimeError("collector exploded"))
        d = dispatchers(sender, senders=1)
        d.submit({"n": 1})
        _wait_for(lambda: d.stats["failed"] >= 1)

        # The thread is still alive and still draining, which is the point.
        sender.result = True
        d.submit({"n": 2})
        _wait_for(lambda: d.stats["delivered"] >= 1)


class TestBoundedQueue:
    def test_full_queue_drops_the_oldest_and_keeps_the_newest(self, dispatchers):
        """Memory is bounded, and what survives is the most recent data."""
        block = threading.Event()
        sender = _Sender()
        original = sender.__call__

        def blocked(event):
            block.wait(5)
            return original(event)

        d = dispatchers(blocked, senders=1, queue_max=2)
        for i in range(20):
            d.submit({"n": i})

        assert d.stats["dropped_queue_full"] > 0
        assert d._queue.qsize() <= 2

        block.set()
        assert d.flush() is True
        _wait_for(lambda: len(sender.sent) >= 2)
        delivered = [e["n"] for e in sender.sent]
        # The newest event survived; the ones dropped were the oldest.
        assert 19 in delivered
        assert len(delivered) < 20

    def test_a_nonsense_queue_size_still_bounds_the_queue(self, dispatchers):
        """queue.Queue treats maxsize <= 0 as unbounded; we must not."""
        d = dispatchers(_Sender(), senders=1, queue_max=0)
        assert d._queue.maxsize == telemetry_dispatch.DEFAULT_QUEUE_MAX

    def test_drops_are_logged_once(self, dispatchers, caplog):
        block = threading.Event()
        d = dispatchers(lambda e: block.wait(5), senders=1, queue_max=1)
        with caplog.at_level("WARNING"):
            for i in range(20):
                d.submit({"n": i})
        block.set()
        warnings = [r for r in caplog.records if "Telemetry queue is full" in r.message]
        assert len(warnings) == 1, "the warning must not repeat per dropped event"


class TestCircuitBreaker:
    def test_opens_after_repeated_failures_and_stops_sending(self, dispatchers):
        sender = _Sender(result=False)
        d = dispatchers(sender, senders=1)

        for i in range(telemetry_dispatch.BREAKER_FAILURES):
            d.submit({"n": i})
        _wait_for(lambda: d._breaker_until > 0)

        attempts = len(sender.sent)
        for i in range(5):
            d.submit({"later": i})
        assert d.flush() is True
        _wait_for(lambda: d.stats["failed"] >= telemetry_dispatch.BREAKER_FAILURES + 5)
        # Events during the cooldown are discarded, not retried.
        assert len(sender.sent) == attempts

    def test_a_success_resets_the_failure_count(self, dispatchers):
        sender = _Sender(result=False)
        d = dispatchers(sender, senders=1)
        d.submit({"n": 1})
        _wait_for(lambda: d.stats["failed"] >= 1)

        sender.result = True
        d.submit({"n": 2})
        _wait_for(lambda: d.stats["delivered"] >= 1)
        assert d._consecutive_failures == 0


class TestSampling:
    def test_sampling_out_drops_before_the_queue(self, dispatchers):
        d = dispatchers(_Sender(), senders=1, sample=0.0)
        assert d.submit({"n": 1}) is False
        assert d.stats["sampled_out"] == 1
        assert d.stats["enqueued"] == 0

    def test_full_sample_keeps_everything(self, dispatchers):
        d = dispatchers(_Sender(), senders=1, sample=1.0)
        assert d.submit({"n": 1}) is True
        assert d.stats["sampled_out"] == 0


class TestConfiguration:
    def test_dispatch_is_the_default(self, monkeypatch):
        monkeypatch.delenv("CB_MCP_TELEMETRY_MODE", raising=False)
        assert dispatch_enabled() is True

    @pytest.mark.parametrize("value", ["legacy", "LEGACY", "Legacy"])
    def test_legacy_mode_disables_dispatch(self, monkeypatch, value):
        monkeypatch.setenv("CB_MCP_TELEMETRY_MODE", value)
        assert dispatch_enabled() is False

    def test_an_unknown_mode_leaves_dispatch_on(self, monkeypatch):
        """Only the documented opt-out word turns it off; typos must not."""
        monkeypatch.setenv("CB_MCP_TELEMETRY_MODE", "disptach")
        assert dispatch_enabled() is True

    def test_env_vars_size_the_queue_and_the_sender_pool(
        self, monkeypatch, dispatchers
    ):
        monkeypatch.setenv("CB_MCP_TELEMETRY_QUEUE", "7")
        monkeypatch.setenv("CB_MCP_TELEMETRY_SENDERS", "3")
        d = dispatchers(_Sender())
        assert d._queue.maxsize == 7
        assert len(d._threads) == 3

    def test_unparseable_env_vars_fall_back_to_defaults(self, monkeypatch, dispatchers):
        """A bad value must not stop the server starting."""
        monkeypatch.setenv("CB_MCP_TELEMETRY_QUEUE", "lots")
        monkeypatch.setenv("CB_MCP_TELEMETRY_SENDERS", "")
        d = dispatchers(_Sender())
        assert d._queue.maxsize == telemetry_dispatch.DEFAULT_QUEUE_MAX
        assert len(d._threads) == telemetry_dispatch.DEFAULT_SENDERS


class TestLifecycle:
    def test_close_is_idempotent(self, dispatchers):
        d = dispatchers(_Sender(), senders=1)
        d.close()
        d.close()
        assert d._stopping.is_set()

    def test_flush_returns_true_when_there_is_nothing_queued(self, dispatchers):
        d = dispatchers(_Sender(), senders=1)
        assert d.flush(timeout=0.5) is True

    def test_flush_reports_false_when_the_queue_cannot_drain(self, dispatchers):
        block = threading.Event()
        d = dispatchers(lambda e: block.wait(5), senders=1, queue_max=50)
        for i in range(20):
            d.submit({"n": i})
        assert d.flush(timeout=0.3) is False
        block.set()

    def test_shutdown_drains_what_is_queued(self, dispatchers):
        """Senders are daemon threads, so a clean exit has to wait for them."""
        sender = _Sender()
        d = dispatchers(sender, senders=1)
        for i in range(5):
            d.submit({"n": i})
        d._at_exit()
        assert len(sender.sent) == 5
        assert d._stopping.is_set()

    def test_senders_stop_after_close(self, dispatchers):
        d = dispatchers(_Sender(), senders=1)
        d.close()
        for t in d._threads:
            t.join(timeout=telemetry_dispatch.POLL_INTERVAL_S * 8)
            assert not t.is_alive()


def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll until a background sender has done its work."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached within timeout")


def test_queue_module_is_used_as_expected():
    """Guard the assumption the bound relies on: maxsize <= 0 is unbounded."""
    assert queue.Queue(maxsize=0).maxsize == 0
