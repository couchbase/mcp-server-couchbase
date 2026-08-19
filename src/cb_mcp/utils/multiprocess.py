"""Multi-worker (multi-process) support for the HTTP transport.

CPython runs bytecode under the GIL, so one MCP server process saturates at
roughly one CPU core regardless of how many cores the host has. FastMCP
executes our synchronous tools on an ``anyio`` thread pool, and those threads
all contend for the same interpreter lock, so neither a larger thread pool nor
a bigger machine raises the ceiling. Throughput scales with *processes*.

``--workers N`` closes that gap: Uvicorn supervises N independent server
processes that share a single listening socket. The parent binds the socket and
hands it to each child, so the kernel load-balances connections and no reverse
proxy is needed. Uvicorn also restarts workers that die and forwards shutdown
signals, which is why we delegate supervision to it rather than forking here.

Two properties of that model shape the helpers below:

* Uvicorn starts workers with the ``spawn`` start method and requires the app
  as an import string, so each worker is a fresh interpreter that never parsed
  our CLI. Resolved options therefore travel to workers through the process
  environment as a JSON document (:func:`export_worker_config` /
  :func:`load_worker_config`). That payload includes the database password when
  one is configured; the exposure is the same as the already-supported
  ``CB_PASSWORD`` environment variable, and strictly better than a
  ``--password`` flag on the command line, which any local user can read from
  the process table.
* Session state cannot live in one worker's memory, because a client's next
  request may be routed to a different worker. Multi-worker mode consequently
  requires FastMCP's stateless HTTP mode
  (see :func:`resolve_worker_settings`).
"""

import json
import os
from collections.abc import Mapping
from typing import Any

from .constants import STREAMABLE_HTTP_TRANSPORT
from .logging import ParsedLogLevel, ParsedLogSinks

# Environment variable carrying the JSON-encoded resolved configuration from
# the supervising parent to each spawned worker. Underscore-prefixed and
# undocumented as a user-facing knob: it is an internal handoff channel, not
# a configuration surface. Operators set the CLI flags / CB_MCP_* variables.
WORKER_CONFIG_ENV_VAR = "_CB_MCP_WORKER_CONFIG"

# Import string Uvicorn resolves in each worker to build the ASGI app. Must
# stay in sync with the factory defined in ``mcp_server``.
WORKER_APP_IMPORT_STRING = "mcp_server:create_app"

# Click param names whose values are not JSON-native and need explicit
# encoding/decoding on the way to a worker.
_LOG_LEVEL_KEY = "log_level"
_LOG_SINKS_KEY = "log_sinks"

# Our log levels mapped onto the names Uvicorn accepts. Uvicorn has no "off",
# so OFF is mapped to its quietest level; our own loggers are silenced
# separately by ``configure_logging``.
_UVICORN_LOG_LEVELS = {
    "OFF": "critical",
    "TRACE": "trace",
    "DEBUG": "debug",
    "INFO": "info",
    "WARNING": "warning",
    "ERROR": "error",
}


class WorkerConfigError(ValueError):
    """Raised when the worker/stateless options cannot be honoured together.

    The CLI entrypoint converts this into a ``click.UsageError`` so the
    operator gets a usage message instead of a traceback, mirroring how
    :class:`cb_mcp.auth.OAuthConfigError` is surfaced.
    """


def resolve_worker_settings(
    workers: int,
    stateless_http: bool | None,
    transport: str,
) -> tuple[int, bool]:
    """Validate the worker options and resolve the effective stateless mode.

    Returns ``(workers, stateless_http)``.

    ``stateless_http`` defaults to ``None`` meaning "decide from the worker
    count": multi-worker requires stateless mode, single-worker keeps the
    stateful default so existing deployments are unaffected. An explicit value
    always wins, except where it would produce a server that cannot work —
    those combinations raise :class:`WorkerConfigError`:

    * ``workers > 1`` on any transport other than streamable HTTP. Only that
      transport has independent, load-balanceable requests; ``stdio`` speaks to
      exactly one client over one pipe pair, and ``sse`` pins a client to the
      process holding its event stream.
    * ``workers > 1`` with ``stateless_http`` explicitly disabled. A session
      created on one worker is unknown to the others, so the client's next
      request would be rejected.
    * ``stateless_http`` enabled on ``sse``, which has no stateless mode
      (FastMCP rejects the combination).

    Enabling stateless mode on ``stdio`` is accepted but inert: the caller
    only forwards it on network transports.
    """
    if workers > 1 and transport != STREAMABLE_HTTP_TRANSPORT:
        raise WorkerConfigError(
            f"--workers={workers} requires --transport={STREAMABLE_HTTP_TRANSPORT}; "
            f"transport '{transport}' cannot be served by multiple processes. "
            "Run a single worker, or switch to the streamable HTTP transport."
        )

    resolved_stateless = workers > 1 if stateless_http is None else stateless_http

    if workers > 1 and not resolved_stateless:
        raise WorkerConfigError(
            f"--workers={workers} requires stateless HTTP: a session created on "
            "one worker process is not visible to the others, so a client's "
            "follow-up request would fail. Remove --stateless-http=false to let "
            "it default to true, or run with --workers=1."
        )

    if resolved_stateless and transport == "sse":
        raise WorkerConfigError(
            "--stateless-http is not supported on --transport=sse; SSE holds a "
            "per-client event stream and therefore requires session state."
        )

    return workers, resolved_stateless


def encode_worker_config(params: Mapping[str, Any]) -> str:
    """Serialise resolved Click params to the JSON handed to workers.

    The two parsed-log params are Click callback results
    (:class:`~cb_mcp.utils.logging.ParsedLogLevel` /
    :class:`~cb_mcp.utils.logging.ParsedLogSinks`) rather than plain values, so
    they are converted to explicit objects. Encoding them implicitly would be
    worse than useless: both are ``NamedTuple`` subclasses, which ``json``
    happily serialises as anonymous arrays that no longer round-trip by field
    name. Every other param is a JSON-native scalar or ``None``; anything else
    raises ``TypeError`` here rather than reaching a worker half-configured.
    """
    payload = dict(params)

    level: ParsedLogLevel = payload[_LOG_LEVEL_KEY]
    payload[_LOG_LEVEL_KEY] = {
        "level": level.level,
        "invalid_token": level.invalid_token,
    }

    sinks: ParsedLogSinks = payload[_LOG_SINKS_KEY]
    payload[_LOG_SINKS_KEY] = {
        "sinks": sorted(sinks.sinks),
        "invalid_tokens": list(sinks.invalid_tokens),
    }

    return json.dumps(payload)


def decode_worker_config(raw: str) -> dict[str, Any]:
    """Rebuild the params mapping produced by :func:`encode_worker_config`.

    The result is shaped exactly like ``click.Context.params`` in the parent,
    so a worker can replay the parent's configuration steps unchanged instead
    of re-deriving them from a second, worker-only code path.
    """
    payload = json.loads(raw)

    level = payload[_LOG_LEVEL_KEY]
    payload[_LOG_LEVEL_KEY] = ParsedLogLevel(level["level"], level["invalid_token"])

    sinks = payload[_LOG_SINKS_KEY]
    payload[_LOG_SINKS_KEY] = ParsedLogSinks(
        set(sinks["sinks"]), list(sinks["invalid_tokens"])
    )

    return payload


def export_worker_config(params: Mapping[str, Any]) -> None:
    """Publish the resolved configuration for workers spawned after this call.

    Writes into this process's environment, which spawned children inherit.
    """
    os.environ[WORKER_CONFIG_ENV_VAR] = encode_worker_config(params)


def load_worker_config() -> dict[str, Any]:
    """Read the configuration published by the supervising parent.

    Raises ``RuntimeError`` when the variable is absent, which means the ASGI
    factory was imported by something other than our own ``--workers``
    supervisor (for example a hand-rolled ``uvicorn mcp_server:create_app``
    invocation). Failing loudly beats starting a worker that silently falls
    back to defaults and connects nowhere.
    """
    raw = os.environ.get(WORKER_CONFIG_ENV_VAR)
    if not raw:
        raise RuntimeError(
            f"{WORKER_CONFIG_ENV_VAR} is not set, so this worker has no "
            "configuration to start from. The ASGI factory is populated by the "
            "server's own --workers supervisor; start the server with "
            "'couchbase-mcp-server --transport=http --workers=N' instead of "
            "pointing an external ASGI server at it."
        )
    return decode_worker_config(raw)


def worker_log_file(log_file: str | None, pid: int) -> str | None:
    """Insert ``pid`` into a log file path so each worker writes its own files.

    ``mcp_server.log`` + 4711 -> ``mcp_server.4711.log``, from which
    ``configure_logging`` derives ``mcp_server.4711.info.log`` and friends.

    Workers are separate processes with separate file handles, so pointing them
    at one ``RotatingFileHandler`` path would let them rotate the same file
    concurrently and lose records. A PID suffix keeps each worker's files
    independent. The trade-off is that a restarted worker starts a new set of
    files under its new PID and the previous set stays on disk.
    """
    if not log_file:
        return log_file
    root, ext = os.path.splitext(log_file)
    return f"{root}.{pid}{ext}"


def uvicorn_log_level(level: str) -> str:
    """Map one of our log levels onto the name Uvicorn's config accepts."""
    return _UVICORN_LOG_LEVELS.get(level.upper(), "info")
