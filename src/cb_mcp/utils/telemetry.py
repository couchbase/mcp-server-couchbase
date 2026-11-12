"""
Reo.dev usage telemetry.

Fires best-effort events via the ``reo-census`` SDK: a startup ping (once
per server deployment) recording the transport mode and server, and one event
per tool call (via ``wrap_with_telemetry``) recording the tool name,
server, transport, outcome and duration. Tool arguments, results and error
messages are never reported.

It is fire-and-forget: ``ReoEventLogger.log_event`` never raises, sends on
a daemon thread by default, and respects the SDK's built-in opt-out env vars
(``PACKAGE_TRACKER_ANALYTICS=false``, ``DO_NOT_TRACK``). Everything here is
additionally wrapped so a telemetry failure (e.g. the dependency itself
misbehaving) can never break server startup.
"""

import functools
import inspect
import logging
import time
from importlib.metadata import PackageNotFoundError, version

from .constants import LOGGER_NAMESPACE

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.utils.telemetry")


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


def send_install_ping(transport: str, *, server_id: str) -> None:
    """Fire a best-effort startup event recording the transport and server.

    Every server ships in one distribution, so neither the package name nor
    the version distinguishes them — ``server_id`` is the only field that
    does.
    """
    if telemetry_logger:
        try:
            telemetry_logger.log_event(
                {
                    "activity_type": "mcp_server_start",
                    "transport": transport,
                    "server": server_id,
                }
            )
        except Exception:
            logger.debug("Failed to send startup telemetry ping", exc_info=True)


def send_tool_call_event(
    tool_name: str,
    *,
    server_id: str,
    transport: str,
    success: bool,
    duration_ms: float,
) -> None:
    """Fire a best-effort event for one tool call.

    Only the tool name and call outcome are reported — never arguments,
    results or error text, which can contain customer data.
    """
    if telemetry_logger:
        try:
            telemetry_logger.log_event(
                {
                    "activity_type": "mcp_tool_call",
                    "tool": tool_name,
                    "server": server_id,
                    "transport": transport,
                    "success": success,
                    "duration_ms": round(duration_ms, 1),
                }
            )
        except Exception:
            logger.debug("Failed to send tool-call telemetry event", exc_info=True)


def wrap_with_telemetry(func, *, server_id: str, transport: str):  # type: ignore[no-untyped-def]
    """Wrap a tool function so each call reports one telemetry event.

    ``functools.wraps`` keeps the name, docstring and signature (via
    ``__wrapped__``), so FastMCP still derives the same schema. Exceptions
    propagate unchanged; the event is sent for failures too.
    """

    def report(tool_started: float, success: bool) -> None:
        send_tool_call_event(
            func.__name__,
            server_id=server_id,
            transport=transport,
            success=success,
            duration_ms=(time.monotonic() - tool_started) * 1000,
        )

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
            started = time.monotonic()
            success = False
            try:
                result = await func(*args, **kwargs)
                success = True
                return result
            finally:
                report(started, success)

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        started = time.monotonic()
        success = False
        try:
            result = func(*args, **kwargs)
            success = True
            return result
        finally:
            report(started, success)

    return wrapper
