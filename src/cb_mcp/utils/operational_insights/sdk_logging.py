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
(``OperationalInsightsClusterProvider`` connects lazily). So the one place
that needs to clean up after it is ``connection.connect_to_operational_insights_cluster``,
wrapping that specific call with ``quiesce_new_root_handlers`` below.

This module does not import the SDK itself.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

#: Name the SDK logs under. Not this repo's ``couchbase`` tree at all.
SDK_LOGGER_NAME = "couchbase_operational_insights"

#: The SDK's own env var for its ``configure_logger()`` side effect
#: (``couchbase_operational_insights.protocol.configure_logger``). Surfaced
#: here purely as documentation for operators who go looking for it; this
#: module does not read it.
SDK_LOG_LEVEL_ENV_VAR = "PYCBOI_LOG_LEVEL"


@contextmanager
def quiesce_new_root_handlers() -> Iterator[None]:
    """Undo any handler the wrapped code installs on the bare stdlib root logger.

    Snapshots the root logger's handlers on entry, then on exit removes
    whatever handler is present that was not there on entry — regardless of
    whether the wrapped code raised. Nothing present *before* entry is ever
    touched, so a host application's own root handler is safe even if it was
    attached after this package was imported: what matters is only what
    changed during this specific call, not what existed at some earlier,
    unrelated point in time (e.g. this module's own import).

    Use this around the exact call that can trigger the SDK's side effect
    (``Cluster.create_instance(...)``), not around unrelated code — a wider
    window risks catching a handler something else added for its own
    reasons during the same window.
    """
    root = logging.getLogger()
    before = set(root.handlers)
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in before:
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

    Does not itself touch the bare stdlib root logger — the SDK's
    ``logging.basicConfig()`` side effect only happens at connection time,
    not here (see the module docstring), so that cleanup lives at the one
    call site that actually triggers it: ``connect_to_operational_insights_cluster``.

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
