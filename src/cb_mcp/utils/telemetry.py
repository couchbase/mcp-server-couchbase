"""
Reo.dev usage telemetry.

Fires two best-effort events via the ``reo-census`` SDK:
- a startup ping (once per server process), recording the transport mode
- a tool-call ping (once per tool invocation), recording the tool name,
  success/failure, and duration

Both are fire-and-forget: ``ReoEventLogger.log_event`` never raises, sends on
a daemon thread by default, and respects the SDK's built-in opt-out env vars
(``PACKAGE_TRACKER_ANALYTICS=false``, ``DO_NOT_TRACK``). Everything here is
additionally wrapped so a telemetry failure (e.g. the dependency itself
misbehaving) can never break server startup or a tool call.
"""

import functools
import inspect
import logging
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.telemetry")

_PACKAGE_NAME = "couchbase-mcp-server"

try:
    from reo_census import ReoEventLogger

    try:
        _package_version = version(_PACKAGE_NAME)
    except PackageNotFoundError:
        _package_version = "0.0.0"

    telemetry_logger = ReoEventLogger(
        package_name=_PACKAGE_NAME,
        package_version=_package_version,
    )
except Exception:
    logger.debug("reo-census unavailable; telemetry disabled", exc_info=True)
    telemetry_logger = None


def send_install_ping(transport: str) -> None:
    """Fire a best-effort startup event recording the transport mode."""
    if telemetry_logger:
        try:
            telemetry_logger.log_event(
                {"activity_type": "mcp_server_start", "transport": transport}
            )
        except Exception:
            logger.debug("Failed to send startup telemetry ping", exc_info=True)


def _send_tool_call_event(tool_name: str, success: bool, duration_ms: float) -> None:
    if telemetry_logger:
        try:
            telemetry_logger.log_event(
                {
                    "activity_type": "tool_call",
                    "tool_name": tool_name,
                    "success": "true" if success else "false",
                    "duration_ms": f"{duration_ms:.1f}",
                }
            )
        except Exception:
            logger.debug("Failed to send tool-call telemetry ping", exc_info=True)


def wrap_with_telemetry(fn: Callable) -> Callable:
    """Wrap a tool function to emit a Reo.dev event on every invocation.

    Fires once per call, after the tool has actually run, regardless of
    whether it succeeded or raised. Applied as the innermost wrapper (before
    confirmation/scope-check wrapping) so the recorded duration/success
    reflects only the tool's own execution.

    When telemetry is unavailable (``telemetry_logger`` is ``None`` at wrap time), the
    original function is returned unchanged rather than a wrapper that would
    just do timing work for an event that never sends.
    """
    if telemetry_logger is None:
        return fn

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            started = time.monotonic()
            success = True
            try:
                return await fn(*args, **kwargs)
            except Exception:
                success = False
                raise
            finally:
                duration_ms = (time.monotonic() - started) * 1000
                _send_tool_call_event(fn.__name__, success, duration_ms)

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        started = time.monotonic()
        success = True
        try:
            return fn(*args, **kwargs)
        except Exception:
            success = False
            raise
        finally:
            duration_ms = (time.monotonic() - started) * 1000
            _send_tool_call_event(fn.__name__, success, duration_ms)

    return sync_wrapper
