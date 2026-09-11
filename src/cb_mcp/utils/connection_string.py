"""Helpers for parsing Couchbase connection strings and deriving REST-call settings from them."""

import logging
import os
from collections.abc import Mapping
from importlib.resources import files
from typing import Any
from urllib.parse import urlparse

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.connection_string")


def validate_connection_settings(settings: Mapping[str, Any]) -> None:
    """Validate that required connection settings are present."""
    required = ["connection_string", "username", "password"]
    missing = [key for key in required if not settings.get(key)]
    if missing:
        raise ValueError(f"Missing required connection settings: {', '.join(missing)}")


def extract_hosts_from_connection_string(connection_string: str) -> list[str]:
    """Extract hosts from a connection string, e.g. 'couchbase://host1,host2'.

    Each comma-separated host:port entry is parsed with urlparse (rather than a naive
    str.split(":")) so IPv6 literals (e.g. '[::1]:8091') and userinfo (e.g. 'user:pass@host')
    are handled correctly. Empty/unparseable entries are dropped, so a malformed or empty
    connection string yields [] rather than a bogus placeholder host.
    """
    hosts = []
    for host_port in urlparse(connection_string).netloc.split(","):
        hostname = urlparse(f"//{host_port}").hostname
        if hostname:
            hosts.append(hostname)
    return hosts


def is_capella_connection(connection_string: str) -> bool:
    """Whether every host in *connection_string* is a Capella host."""
    hosts = extract_hosts_from_connection_string(connection_string)
    return bool(hosts) and all(
        host.lower().endswith(".cloud.couchbase.com") for host in hosts
    )


def _get_capella_root_ca_path() -> str:
    """Get the path to the Capella root CA certificate.

    Uses importlib.resources to locate the certificate file, which works when the package is installed with fallback for development.

    Returns:
        Path to the Capella root CA certificate file.
    """
    try:
        # Use importlib.resources to get the certificate path (works for installed packages)
        cert_file = files("cb_mcp.certs").joinpath("capella_root_ca.pem")
        # Convert to string path - this works for both installed packages and dev mode
        return str(cert_file)
    except (ImportError, FileNotFoundError, TypeError):
        # Fallback for development: use src/certs/ directory
        utils_dir = os.path.dirname(os.path.abspath(__file__))
        src_dir = os.path.dirname(utils_dir)
        fallback_path = os.path.join(src_dir, "certs", "capella_root_ca.pem")

        if os.path.exists(fallback_path):
            logger.info(f"Using fallback certificate path: {fallback_path}")
            return fallback_path

        # If we still can't find it, log a warning and return the fallback path anyway
        logger.warning(
            f"Could not locate Capella root CA certificate at {fallback_path}. "
            "SSL verification may fail for Capella connections."
        )
        return fallback_path


def determine_ssl_verification(
    connection_string: str, ca_cert_path: str | None
) -> bool | str:
    """Determine SSL verification setting based on connection string and cert path.

    Args:
        connection_string: Couchbase connection string
        ca_cert_path: Optional path to CA certificate

    Returns:
        SSL verification setting (bool or path to cert file)
    """
    is_tls_enabled = connection_string.lower().startswith("couchbases://")

    # Priority 1: Capella connections always use Capella root CA
    if is_capella_connection(connection_string):
        capella_ca = _get_capella_root_ca_path()
        if os.path.exists(capella_ca):
            logger.info(
                f"Capella connection detected, using Capella root CA: {capella_ca}"
            )
            return capella_ca
        logger.warning(
            f"Capella CA certificate not found at {capella_ca}, "
            "falling back to system CA bundle"
        )
        return True

    # Priority 2: Non-Capella TLS connections use provided cert or system CA bundle
    if is_tls_enabled:
        if ca_cert_path:
            logger.info(f"Using provided CA certificate: {ca_cert_path}")
            return ca_cert_path
        logger.info("Using system CA bundle for SSL verification")
        return True

    # Priority 3: Non-TLS connections (HTTP), disable SSL verification
    logger.info("Non-TLS connection, SSL verification disabled")
    return False
