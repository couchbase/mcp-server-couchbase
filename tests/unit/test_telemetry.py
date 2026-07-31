"""Tests for Reo.dev telemetry: startup ping and per-tool-call wrapper.

Coverage map:
- send_install_ping fires one event with the transport mode, never raises.
- wrap_with_telemetry fires one event per call with tool_name/success/duration,
  for both sync and async tools, and re-raises exceptions from the wrapped tool
  after still recording the failed call.
"""

import pytest

from cb_mcp.utils import telemetry


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

        assert len(fake_logger.events) == 1
        assert fake_logger.events[0]["success"] == "false"

    @pytest.mark.asyncio
    async def test_noop_logger_does_not_prevent_execution(self, monkeypatch):
        monkeypatch.setattr(telemetry, "telemetry_logger", None)

        def sample_tool() -> str:
            return "ok"

        wrapped = telemetry.wrap_with_telemetry(sample_tool)
        assert wrapped() == "ok"
