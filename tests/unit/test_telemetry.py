"""Tests for Reo.dev telemetry: the startup ping is the only event.

Coverage map:
- send_install_ping fires one event with the transport mode, never raises.
- tool calls emit nothing: there is no per-call wrapper to install.
"""

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


def test_tool_calls_are_not_reported():
    """Registered tools are the plain functions: no telemetry wrapper, no event."""
    assert not hasattr(telemetry, "wrap_with_telemetry")
    assert not hasattr(telemetry, "_send_tool_call_event")
