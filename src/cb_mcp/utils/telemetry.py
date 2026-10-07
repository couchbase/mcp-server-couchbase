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
from .telemetry_dispatch import EventDispatcher, dispatch_enabled

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

# Delivery of tool-call events. reo-census starts one thread and one
# connection per event, which is sized for an occasional install ping rather
# than for one event per tool call; the dispatcher keeps the same events and
# the same sender, and only moves the send onto a long-lived thread. See
# telemetry_dispatch for the env vars, including CB_MCP_TELEMETRY_MODE=legacy
# to restore the original path.
_dispatcher: EventDispatcher | None = None


def _get_dispatcher() -> EventDispatcher | None:
    """Build the dispatcher on first use, or None when it is not wanted."""
    global _dispatcher  # noqa: PLW0603 - one dispatcher per process, built lazily
    if telemetry_logger is None or not dispatch_enabled():
        return None
    if _dispatcher is None:
        try:
            # blocking=True keeps reo-census's endpoint resolution, opt-out
            # check and payload handling, and only stops it spawning a thread.
            _dispatcher = EventDispatcher(
                send_one=lambda event: telemetry_logger.log_event(event, blocking=True),
            )
        except Exception:
            logger.debug("telemetry dispatcher unavailable", exc_info=True)
            return None
    return _dispatcher


def flush_telemetry(timeout: float = 2.0) -> bool:
    """Wait for queued tool-call events to be handed to a sender.

    Delivery is asynchronous, so anything that needs to observe an event
    (tests, or a clean shutdown) has to wait for the queue to drain.
    """
    dispatcher = _dispatcher
    return dispatcher.flush(timeout) if dispatcher is not None else True


def telemetry_status() -> dict:
    """What telemetry is doing right now, for the diagnostics tool.

    Delivery is best-effort and events can be dropped, so an operator needs a
    way to see whether that is happening rather than only a one-time log line.
    Returns ``enabled: False`` when no logger is configured, which is also what
    an opt-out looks like.
    """
    if telemetry_logger is None:
        return {"enabled": False, "delivery": "disabled"}
    dispatcher = _dispatcher
    if dispatcher is None:
        return {
            "enabled": True,
            "delivery": "dispatch" if dispatch_enabled() else "legacy",
            "counters": None,
        }
    stats = dict(dispatcher.stats)
    # The counters are there so an operator can see loss, so do the division
    # for them: "delivered 11% of what the tools produced" is the number that
    # tells you the sender count is wrong for this collector's distance, and
    # it is not obvious from four raw counters.
    produced = stats["enqueued"] + stats["dropped_queue_full"] + stats["sampled_out"]
    return {
        "enabled": True,
        "delivery": "dispatch",
        "senders": len(dispatcher._threads),
        "queue_max": dispatcher._queue.maxsize,
        "queue_depth": dispatcher._queue.qsize(),
        "counters": stats,
        "delivered_pct": (
            round(stats["delivered"] / produced * 100, 1) if produced else None
        ),
    }


def reset_telemetry_dispatcher() -> None:
    """Drop the dispatcher so the next event rebuilds it.

    Needed when ``telemetry_logger`` is replaced after the dispatcher was
    built, which is what tests do when they swap in a recording logger.
    """
    global _dispatcher
    dispatcher, _dispatcher = _dispatcher, None
    if dispatcher is not None:
        dispatcher.close()


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
    if not telemetry_logger:
        return
    event = {
        "activity_type": "tool_call",
        "tool_name": tool_name,
        "success": "true" if success else "false",
        "duration_ms": f"{duration_ms:.1f}",
    }
    try:
        dispatcher = _get_dispatcher()
        if dispatcher is not None:
            dispatcher.submit(event)
        else:
            telemetry_logger.log_event(event)
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
