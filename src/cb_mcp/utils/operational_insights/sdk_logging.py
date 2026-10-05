"""Bridge the Operational Insights SDK's own logging into this server's hierarchy.

``couchbase_operational_insights`` logs under its own logger name
(``couchbase_operational_insights``), entirely outside this repo's
``couchbase`` handler root (see ``utils.constants.LOGGER_ROOT``), so its
records would otherwise land in no configured log file.

This module does not import the SDK itself.
"""

import logging

#: Name the SDK logs under. Not this repo's ``couchbase`` tree at all.
SDK_LOGGER_NAME = "couchbase_operational_insights"


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

    Does not itself touch the bare stdlib root logger — the SDK only ever
    configures its own loggers (``configure_logging_from_env`` in
    ``couchbase_operational_insights.common.logging``), never the root.

    Records forwarded this way keep ``%(name)s`` as
    ``couchbase_operational_insights.*``, not this server's own namespace —
    that is deliberate, matching how the operational SDK's own records
    appear under their own name rather than being relabelled.
    """
    sdk_logger = logging.getLogger(SDK_LOGGER_NAME)
    for handler in list(sdk_logger.handlers):
        sdk_logger.removeHandler(handler)
        handler.close()
    sdk_logger.setLevel(level)
    # We forward explicitly below; don't also let records reach the bare
    # stdlib root a second time via propagation.
    sdk_logger.propagate = False
    sdk_logger.addHandler(_ForwardingHandler(logger_root))
