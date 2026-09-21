"""Bridge the Operational Insights SDK's own logging into this server's hierarchy.

``couchbase_operational_insights`` logs under its own logger name
(``couchbase_operational_insights``), entirely outside this repo's
``couchbase`` handler root (see ``utils.constants.LOGGER_ROOT``), so its
records would otherwise land in no configured log file.

The SDK also runs ``configure_logger()``
(``couchbase_operational_insights.protocol``), which can call
``logging.basicConfig()`` on the bare stdlib root logger — a side effect
that has nothing to do with this server's own logging setup. This does NOT
happen at import time of the top-level package (verified empirically): the
``protocol`` submodule, and therefore this side effect, is only pulled in
the first time ``Cluster.create_instance(...)`` actually runs — i.e. at
connection time, which for the standalone host is on the *first tool call*
(``OperationalInsightsClusterProvider`` connects lazily). So this module's
cleanup function is called from two places: the ``sdk_log_hook`` (for the
common case where nothing has connected yet) and
``connection.connect_to_operational_insights_cluster`` itself, in a
``finally`` block, so a stray handler installed by the *first* connection
attempt is still cleaned up even though it necessarily happens after that
hook already ran once at startup.

This module does not import the SDK itself.
"""

import logging

#: Name the SDK logs under. Not this repo's ``couchbase`` tree at all.
SDK_LOGGER_NAME = "couchbase_operational_insights"

#: The SDK's own env var for its ``configure_logger()`` side effect
#: (``couchbase_operational_insights.protocol.configure_logger``). Surfaced
#: here purely as documentation for operators who go looking for it; this
#: module does not read it.
SDK_LOG_LEVEL_ENV_VAR = "PYCBOI_LOG_LEVEL"

# Snapshot taken at import time of *this module* — as early as this package
# can arrange, since cb_mcp.utils.operational_insights imports this module
# first, before any sibling submodule that imports the SDK. In practice the
# SDK's side effect fires later still (at connection time, not import time —
# see the module docstring), but taking the snapshot this early costs
# nothing and guards against a future SDK version moving the side effect
# earlier.
_ROOT_HANDLERS_AT_IMPORT: tuple[logging.Handler, ...] = tuple(
    logging.getLogger().handlers
)


def quiesce_sdk_root_logging() -> None:
    """Remove any handler the SDK's ``configure_logger()`` added to the root.

    Only removes handlers that were not present when this module was first
    imported — anything a host application deliberately attached to the
    bare root logger before importing this package is left alone. Safe to
    call more than once; call it again after connecting, since that is when
    the SDK's side effect actually happens (see the module docstring).
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler not in _ROOT_HANDLERS_AT_IMPORT:
            root.removeHandler(handler)
            handler.close()


class _ForwardingHandler(logging.Handler):
    """Re-emit a record through another logger's handlers, resolved per call.

    Late-binding (looking the target logger up by name on every ``emit``,
    rather than copying its handler objects once) is deliberate:
    ``configure_logging`` calls the SDK hook *after* attaching handlers on
    the normal path but *before* attaching them on the ``level="OFF"`` path,
    and it is safe to call repeatedly (tests do). Resolving the target by
    name each time is correct in every one of those cases; copying handler
    references once could not be.

    Calls ``callHandlers`` directly rather than ``handle`` — deliberately,
    not an oversight. Python 3.13 added a thread-local re-entrancy guard to
    ``Logger.handle`` (``self._tls.in_progress``) to stop a handler from
    recursively re-triggering the *same* logger it's attached to. That guard
    is shared across every ``Logger`` instance on the thread (verified:
    ``getLogger("a")._tls is getLogger("b")._tls``), so it also silently
    swallows this forwarder's call to a *different* logger's ``handle()``
    while the source logger's own ``handle()`` call is still on the stack —
    ``callHandlers`` skips the disabled/filter checks ``handle()`` layers on
    top, which we don't need a second time here anyway (the source logger's
    ``handle()`` already ran them for this record).
    """

    def __init__(self, target_logger_name: str) -> None:
        super().__init__()
        self._target_logger_name = target_logger_name

    def emit(self, record: logging.LogRecord) -> None:
        logging.getLogger(self._target_logger_name).callHandlers(record)


def bridge_sdk_logging(logger_root: str, level: int) -> None:
    """``ServerSpec.sdk_log_hook`` for the Operational Insights server.

    Routes the SDK's ``couchbase_operational_insights.*`` records into the
    handlers already attached at ``logger_root`` (this server's configured
    ``couchbase`` hierarchy), so SDK records land in the same log files as
    everything else — parity with what the operational server gets from
    ``couchbase.configure_logging``.

    Also cleans up any stray handler the SDK's own ``logging.basicConfig()``
    side effect may already have left on the bare root logger from an
    earlier connection (see the module docstring).

    Records forwarded this way keep ``%(name)s`` as
    ``couchbase_operational_insights.*``, not this server's own namespace —
    that is deliberate, matching how the operational SDK's own records
    appear under their own name rather than being relabelled.
    """
    quiesce_sdk_root_logging()

    sdk_logger = logging.getLogger(SDK_LOGGER_NAME)
    for handler in list(sdk_logger.handlers):
        sdk_logger.removeHandler(handler)
        handler.close()
    sdk_logger.setLevel(level)
    # We forward explicitly below; don't also let records reach the bare
    # stdlib root a second time via propagation.
    sdk_logger.propagate = False
    sdk_logger.addHandler(_ForwardingHandler(logger_root))
