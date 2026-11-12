"""Tests for Reo.dev telemetry: the startup ping and per-tool-call events.

Coverage map:
- send_install_ping fires one event with the transport mode, never raises.
- wrap_with_telemetry emits one event per tool call (success or failure)
  carrying only the tool name, never arguments or error text.
"""

import inspect

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

        telemetry.send_install_ping("stdio", server_id="operational")

        assert len(fake_logger.events) == 1
        assert fake_logger.events[0]["activity_type"] == "mcp_server_start"
        assert fake_logger.events[0]["transport"] == "stdio"

    def test_noop_when_logger_unavailable(self, monkeypatch):
        monkeypatch.setattr(telemetry, "telemetry_logger", None)
        # Must not raise even with no logger configured.
        telemetry.send_install_ping("http", server_id="operational")

    def test_swallows_logger_exceptions(self, monkeypatch):
        class BrokenLogger:
            def log_event(self, *args, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(telemetry, "telemetry_logger", BrokenLogger())
        # Must not raise even when the underlying SDK call blows up.
        telemetry.send_install_ping("stdio", server_id="operational")


class TestWrapWithTelemetry:
    @staticmethod
    def _wrap(**kw):
        def tool(q: str, limit: int = 5):
            """Docstring."""
            return f"ran {q}"

        return tool, telemetry.wrap_with_telemetry(
            tool, server_id="operational", transport="stdio", **kw
        )

    def test_preserves_metadata_and_signature(self):
        tool, wrapped = self._wrap()
        assert wrapped.__name__ == "tool"
        assert wrapped.__doc__ == "Docstring."
        assert inspect.signature(wrapped) == inspect.signature(tool)

    def test_reports_successful_call(self, monkeypatch):
        fake_logger = _RecordingLogger()
        monkeypatch.setattr(telemetry, "telemetry_logger", fake_logger)
        _, wrapped = self._wrap()

        assert wrapped("secret") == "ran secret"

        (event,) = fake_logger.events
        assert event["activity_type"] == "mcp_tool_call"
        assert event["tool"] == "tool"
        assert event["server"] == "operational"
        assert event["transport"] == "stdio"
        assert event["success"] is True
        assert "secret" not in str(event)

    def test_reports_failed_call_and_reraises(self, monkeypatch):
        fake_logger = _RecordingLogger()
        monkeypatch.setattr(telemetry, "telemetry_logger", fake_logger)

        def failing():
            raise ValueError("secret failure detail")

        wrapped = telemetry.wrap_with_telemetry(
            failing, server_id="operational", transport="http"
        )
        with pytest.raises(ValueError, match="secret failure detail"):
            wrapped()

        (event,) = fake_logger.events
        assert event["tool"] == "failing"
        assert event["success"] is False
        assert "secret" not in str(event)

    @pytest.mark.asyncio
    async def test_async_tools_are_awaited_and_reported(self, monkeypatch):
        fake_logger = _RecordingLogger()
        monkeypatch.setattr(telemetry, "telemetry_logger", fake_logger)

        async def atool():
            return "ok"

        wrapped = telemetry.wrap_with_telemetry(
            atool, server_id="operational", transport="stdio"
        )
        assert await wrapped() == "ok"
        assert fake_logger.events[0]["success"] is True

    def test_telemetry_failure_does_not_break_call(self, monkeypatch):
        class BrokenLogger:
            def log_event(self, *args, **kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(telemetry, "telemetry_logger", BrokenLogger())
        _, wrapped = self._wrap()
        assert wrapped("x") == "ran x"
