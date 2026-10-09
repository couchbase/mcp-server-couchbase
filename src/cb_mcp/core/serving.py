"""How a server is served: process count, HTTP session mode, tool concurrency.

CPython runs bytecode under the GIL, so one server process saturates at about
one CPU core however many the host has. FastMCP runs our synchronous tools on
AnyIO's thread pool, and those threads all contend for the same interpreter
lock, so neither a larger pool nor a bigger machine raises that ceiling.
Throughput scales with *processes*: ``--workers N`` has Uvicorn supervise N
server processes sharing one listening socket.

Two consequences shape this module:

* **Multiple workers need stateless HTTP.** The kernel balances
  *connections* across workers, not MCP sessions, so a session created on one
  worker is unknown to the next request's worker. Stateless mode handles each
  request with a fresh transport and keeps no session.
* **Stateless HTTP has no session to elicit through.** Each request gets an
  uninitialized session with no client capabilities, so confirmation prompts
  cannot be shown (``wrap_with_confirmation`` would run the tool unconfirmed)
  and the client's reply would have nowhere to land. Configuring
  confirmation-required tools together with stateless mode is therefore
  rejected, not degraded.

Everything here is pure: no environment, no Click, no process spawning. The
host (``src/mcp_server.py``) resolves flags into arguments, converts
:class:`ServingConfigError` into a usage error, and owns the worker handoff.
"""

import logging
import os
import re
from collections.abc import Collection
from typing import NamedTuple

from anyio.to_thread import current_default_thread_limiter

from ..utils.constants import (
    LOGGER_NAMESPACE,
    NETWORK_TRANSPORTS,
    STREAMABLE_HTTP_TRANSPORT,
)

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.core.serving")

# Our log levels mapped onto the names Uvicorn accepts. Uvicorn has no "off",
# so OFF maps to its quietest level; our own loggers are silenced separately
# by configure_logging.
_UVICORN_LOG_LEVELS = {
    "OFF": "critical",
    "TRACE": "trace",
    "DEBUG": "debug",
    "INFO": "info",
    "WARNING": "warning",
    "ERROR": "error",
}

# Anything outside this set in a hostname is replaced before it becomes part
# of a file name.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


class ServingConfigError(ValueError):
    """The serving options cannot be honoured together.

    The host converts this into a usage error, so the operator gets a message
    rather than a traceback, mirroring ``cb_mcp.auth.OAuthConfigError``.
    """


class ServingConfig(NamedTuple):
    """The resolved serving topology for one server.

    ``stateless_http`` is always a concrete bool here: ``True`` only on the
    streamable HTTP transport. ``thread_pool_size`` stays ``None`` when the
    operator left it to the runtime default.
    """

    workers: int
    stateless_http: bool
    thread_pool_size: int | None


def resolve_serving(
    *,
    server_id: str,
    transport: str,
    workers: int,
    stateless_http: bool | None,
    thread_pool_size: int | None,
    supports_multiple_workers: bool,
    confirmation_required: Collection[str],
    runtime_default_stateless: bool = False,
) -> ServingConfig:
    """Resolve the serving options, or raise :class:`ServingConfigError`.

    ``stateless_http`` of ``None`` means "not set by the operator": stateless
    when ``workers > 1``, otherwise whatever the HTTP runtime would do on its
    own — ``runtime_default_stateless``, which the host reads from FastMCP's
    settings (``FASTMCP_STATELESS_HTTP``). Folding that default in here is
    what keeps a deployment that already set FastMCP's variable working
    unchanged, while the checks below and the diagnostic record see the mode
    that will actually run. ``confirmation_required`` is the *resolved* set
    from tool gating, so an empty or entirely invalid list never trips the
    check.

    Every contradictory combination is an error rather than a silent
    override: quietly dropping to one worker would hand the operator a
    fraction of the capacity they asked for, and quietly ignoring an explicit
    ``--stateless-http false`` would hide a configuration they wrote down.
    The exceptions are inert settings, warned about and ignored: stateless
    mode on stdio, and an *inherited* runtime default on SSE (an explicit
    ``--stateless-http`` on SSE is still an error).
    """
    if workers > 1:
        if transport != STREAMABLE_HTTP_TRANSPORT:
            raise ServingConfigError(
                f"--workers={workers} requires "
                f"--transport={STREAMABLE_HTTP_TRANSPORT}; transport "
                f"'{transport}' cannot be served by multiple processes."
            )
        if not supports_multiple_workers:
            raise ServingConfigError(
                f"--workers={workers} is not supported by the '{server_id}' "
                "server: it keeps state in process memory that other worker "
                "processes cannot see. Run it with --workers=1."
            )
        if stateless_http is False:
            raise ServingConfigError(
                f"--workers={workers} requires stateless HTTP, because a "
                "session created on one worker process is unknown to the "
                "others. Remove --stateless-http=false, or use --workers=1 to "
                "keep session state."
            )

    explicit = stateless_http is not None
    if explicit:
        resolved_stateless = bool(stateless_http)
        source = "--stateless-http"
    elif workers > 1:
        resolved_stateless = True
        source = f"--workers={workers}"
    else:
        resolved_stateless = runtime_default_stateless
        source = "FASTMCP_STATELESS_HTTP"

    # Inert here: stdio has no HTTP at all, and an inherited runtime default
    # should not turn an SSE server that works today into a startup error.
    inert = transport not in NETWORK_TRANSPORTS or (
        transport != STREAMABLE_HTTP_TRANSPORT and not explicit
    )
    if resolved_stateless and inert:
        logger.warning(
            "%s is only honored for the %s transport; ignoring it for transport=%s.",
            source,
            STREAMABLE_HTTP_TRANSPORT,
            transport,
        )
        resolved_stateless = False

    if resolved_stateless and transport != STREAMABLE_HTTP_TRANSPORT:
        raise ServingConfigError(
            f"--stateless-http requires --transport={STREAMABLE_HTTP_TRANSPORT}; "
            f"transport '{transport}' holds a per-client event stream and has "
            "no stateless mode."
        )

    if resolved_stateless and confirmation_required:
        raise ServingConfigError(
            f"Stateless HTTP (enabled by {source}) cannot be combined with "
            "--confirmation-required-tools "
            f"({', '.join(sorted(confirmation_required))}): confirmation uses "
            "MCP elicitation, which needs a session, and stateless mode keeps "
            "none. Remove the confirmation-required tools (or disable them "
            "with --disabled-tools), or run a single worker without "
            "--stateless-http or FASTMCP_STATELESS_HTTP."
        )

    return ServingConfig(
        workers=workers,
        stateless_http=resolved_stateless,
        thread_pool_size=thread_pool_size,
    )


def worker_log_file(log_file: str, *, host: str, pid: int) -> str:
    """Insert host and pid into a log file path for one worker process.

    ``mcp_server.log`` on host ``web-1`` as pid 4711 becomes
    ``mcp_server.web-1.4711.log``, from which ``configure_logging`` derives
    ``mcp_server.web-1.4711.info.log`` and friends.

    ``RotatingFileHandler`` is not multi-process safe: workers sharing one
    path would rotate the same file concurrently and lose records. The pid
    keeps each worker's files apart; the host keeps replicas apart when the
    log directory is a shared volume. The trade-off is that a restarted
    worker starts a new set of files under its new pid.
    """
    safe_host = _UNSAFE_FILENAME_CHARS.sub("_", host.split(".", 1)[0]) or "host"
    root, ext = os.path.splitext(log_file)
    return f"{root}.{safe_host}.{pid}{ext}"


def uvicorn_log_level(level: str) -> str:
    """Map one of our log levels onto the name Uvicorn's config accepts."""
    return _UVICORN_LOG_LEVELS.get(level.upper(), "info")


def apply_thread_pool_limit(size: int | None) -> int:
    """Set the per-process cap on concurrently executing tool calls.

    FastMCP runs synchronous tools through ``anyio.to_thread.run_sync``, whose
    default ``CapacityLimiter`` therefore caps how many tool calls *execute*
    at once in this process; calls past the cap wait for a slot. Raising it
    helps when calls spend their time blocked on the cluster (high-latency
    links, long timeouts) or slow tools would otherwise hold every slot. It
    does not raise CPU-bound throughput, which is one core per process
    regardless; that is what workers are for.

    ``None`` leaves AnyIO's default in place rather than restating it here.
    Must be called inside the running event loop: the limiter is run-scoped,
    and each worker process has its own. Returns the limit that took effect.
    """
    limiter = current_default_thread_limiter()
    if size is not None:
        limiter.total_tokens = size
    return int(limiter.total_tokens)
