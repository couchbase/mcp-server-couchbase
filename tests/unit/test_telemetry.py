"""Tests for Reo.dev telemetry: startup ping and per-tool-call wrapper.

Coverage map:
- send_install_ping fires one event with the transport mode, never raises.
- wrap_with_telemetry fires one event per call with tool_name/success/duration,
  for both sync and async tools, and re-raises exceptions from the wrapped tool
  after still recording the failed call.
"""

import pytest

from cb_mcp.utils import telemetry


@pytest.fixture(autouse=True)
def _reset_dispatcher():
    """Tool-call events are delivered asynchronously by a shared dispatcher.

    Rebuild it around each test so it picks up the logger the test installs,
    and tear it down afterwards.
    """
    telemetry.reset_telemetry_dispatcher()
    yield
    telemetry.reset_telemetry_dispatcher()


class _RecordingLogger:
    """Stand-in for ReoEventLogger.log_event that records calls instead of sending."""

    def __init__(self):
        self.events = []

    def log_event(self, properties=None, **kwargs):
        self.events.append(properties or {})
        return True


class TestSendInstallPing:
    def test_fires_one_event_with_transport(self, monkeypatch):
        fake_logger = _RecordingLogger()
        monkeypatch.setattr(telemetry, "telemetry_logger", fake_logger)

        telemetry.send_install_ping("stdio")

        assert len(fake_logger.events) == 1
        assert fake_logger.events[0]["activity_type"] == "mcp_server_start"
        assert fake_logger.events[0]["transport"] == "stdio"

    def test_noop_when_logger_unavailable(self, monkeypatch):
        monkeypatch.setattr(telemetry, "telemetry_logger", None)
        # Must not raise even with no logger configured.
        telemetry.send_install_ping("http")

    def test_swallows_logger_exceptions(self, monkeypatch):
        class BrokenLogger:
            def log_event(self, *args, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(telemetry, "telemetry_logger", BrokenLogger())
        # Must not raise even when the underlying SDK call blows up.
        telemetry.send_install_ping("stdio")


class TestTelemetryStatus:
    """``telemetry_status`` is how an operator sees that events are dropping.

    The counters are the only signal available once the one-time warning has
    scrolled away, so these pin the shape of what the diagnostics tool reads.
    """

    def test_reports_disabled_when_no_logger(self, monkeypatch):
        monkeypatch.setattr(telemetry, "telemetry_logger", None)
        assert telemetry.telemetry_status() == {
            "enabled": False,
            "delivery": "disabled",
        }

    def test_reports_legacy_delivery_without_building_a_dispatcher(self, monkeypatch):
        monkeypatch.setenv("CB_MCP_TELEMETRY_MODE", "legacy")
        monkeypatch.setattr(telemetry, "telemetry_logger", _RecordingLogger())
        status = telemetry.telemetry_status()
        assert status["enabled"] is True
        assert status["delivery"] == "legacy"
        assert status["counters"] is None

    def test_counters_appear_once_events_have_been_sent(self, monkeypatch):
        fake_logger = _RecordingLogger()
        monkeypatch.setattr(telemetry, "telemetry_logger", fake_logger)

        wrapped = telemetry.wrap_with_telemetry(lambda: "ok")
        wrapped()
        assert telemetry.flush_telemetry()

        status = telemetry.telemetry_status()
        assert status["delivery"] == "dispatch"
        assert status["counters"]["enqueued"] == 1
        assert status["counters"]["dropped_queue_full"] == 0
        assert status["queue_max"] > 0
        assert status["senders"] >= 1
        # The derived figure is the one an operator reads: everything the
        # tools produced was delivered here.
        assert status["delivered_pct"] == 100.0

    def test_delivered_pct_counts_drops_and_sampling_as_not_delivered(
        self, monkeypatch
    ):
        """A sender count too low for the collector's distance shows up here as
        a low percentage, which is the whole point of reporting it."""
        monkeypatch.setattr(telemetry, "telemetry_logger", _RecordingLogger())
        dispatcher = telemetry._get_dispatcher()
        assert dispatcher is not None
        dispatcher.stats.update(
            {
                "enqueued": 10,
                "delivered": 10,
                "dropped_queue_full": 80,
                "sampled_out": 10,
            }
        )
        assert telemetry.telemetry_status()["delivered_pct"] == 10.0

    def test_delivered_pct_is_none_before_any_event(self, monkeypatch):
        monkeypatch.setattr(telemetry, "telemetry_logger", _RecordingLogger())
        telemetry._get_dispatcher()
        assert telemetry.telemetry_status()["delivered_pct"] is None


class TestWrapWithTelemetry:
    @pytest.mark.asyncio
    async def test_sync_tool_fires_success_event(self, monkeypatch):
        fake_logger = _RecordingLogger()
        monkeypatch.setattr(telemetry, "telemetry_logger", fake_logger)

        def sample_tool(x: int) -> int:
            return x * 2

        wrapped = telemetry.wrap_with_telemetry(sample_tool)
        result = wrapped(21)

        assert result == 42
        assert telemetry.flush_telemetry()
        assert len(fake_logger.events) == 1
        event = fake_logger.events[0]
        assert event["activity_type"] == "tool_call"
        assert event["tool_name"] == "sample_tool"
        assert event["success"] == "true"
        assert "duration_ms" in event

    @pytest.mark.asyncio
    async def test_async_tool_is_awaited_and_fires_success_event(self, monkeypatch):
        fake_logger = _RecordingLogger()
        monkeypatch.setattr(telemetry, "telemetry_logger", fake_logger)
        called = False

        async def async_sample_tool() -> bool:
            nonlocal called
            called = True
            return True

        wrapped = telemetry.wrap_with_telemetry(async_sample_tool)
        result = await wrapped()

        assert called is True
        assert result is True
        assert telemetry.flush_telemetry()
        assert fake_logger.events[0]["tool_name"] == "async_sample_tool"
        assert fake_logger.events[0]["success"] == "true"

    @pytest.mark.asyncio
    async def test_exception_is_reraised_and_recorded_as_failure(self, monkeypatch):
        fake_logger = _RecordingLogger()
        monkeypatch.setattr(telemetry, "telemetry_logger", fake_logger)

        def failing_tool():
            raise ValueError("bad input")

        wrapped = telemetry.wrap_with_telemetry(failing_tool)

        with pytest.raises(ValueError, match="bad input"):
            wrapped()

        assert telemetry.flush_telemetry()
        assert len(fake_logger.events) == 1
        assert fake_logger.events[0]["success"] == "false"

    @pytest.mark.asyncio
    async def test_legacy_mode_sends_on_the_calling_thread(self, monkeypatch):
        """``CB_MCP_TELEMETRY_MODE=legacy`` restores reo-census's own delivery,
        so the event is visible without flushing anything."""
        monkeypatch.setenv("CB_MCP_TELEMETRY_MODE", "legacy")
        fake_logger = _RecordingLogger()
        monkeypatch.setattr(telemetry, "telemetry_logger", fake_logger)

        wrapped = telemetry.wrap_with_telemetry(lambda: "ok")
        assert wrapped() == "ok"

        assert len(fake_logger.events) == 1
        assert telemetry._dispatcher is None

    @pytest.mark.asyncio
    async def test_a_broken_logger_does_not_break_the_tool(self, monkeypatch):
        """Telemetry failure must never surface as a tool failure."""

        class BrokenLogger:
            def log_event(self, *args, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(telemetry, "telemetry_logger", BrokenLogger())

        wrapped = telemetry.wrap_with_telemetry(lambda: "ok")
        assert wrapped() == "ok"
        assert telemetry.flush_telemetry()

    def test_flush_is_a_noop_before_any_event(self, monkeypatch):
        """Nothing has built a dispatcher yet, so there is nothing to wait
        for, and callers must not have to care."""
        monkeypatch.setattr(telemetry, "telemetry_logger", _RecordingLogger())
        assert telemetry.flush_telemetry() is True

    @pytest.mark.asyncio
    async def test_noop_logger_does_not_prevent_execution(self, monkeypatch):
        monkeypatch.setattr(telemetry, "telemetry_logger", None)

        def sample_tool() -> str:
            return "ok"

        wrapped = telemetry.wrap_with_telemetry(sample_tool)
        assert wrapped() == "ok"
