"""
Reo.dev usage telemetry.

Fires one best-effort event via the ``reo-census`` SDK: a startup ping (once
per server deployment), recording the transport mode and server. Tool calls
are deliberately not reported.

It is fire-and-forget: ``ReoEventLogger.log_event`` never raises, sends on
a daemon thread by default, and respects the SDK's built-in opt-out env vars
(``PACKAGE_TRACKER_ANALYTICS=false``, ``DO_NOT_TRACK``). Everything here is
additionally wrapped so a telemetry failure (e.g. the dependency itself
misbehaving) can never break server startup.
"""

import logging
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
