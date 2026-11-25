"""Helpers for parsing Couchbase connection strings and deriving REST-call settings from them."""

import logging
import os
import ssl
from collections.abc import Mapping
from importlib.resources import files
from typing import Any
from urllib.parse import urlparse

import httpx

from ...core.spec import Deployment
from ..constants import LOGGER_NAMESPACE

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.utils.connection_string")


def uses_client_cert(settings: Mapping[str, Any]) -> bool:
    """Whether *settings* select client-certificate (mTLS) authentication.

    Mirrors ``connect_to_couchbase_cluster``: the certificate wins over a
    username/password when both are configured.
    """
    return bool(settings.get("client_cert_path") and settings.get("client_key_path"))


def validate_connection_settings(settings: Mapping[str, Any]) -> None:
    """Validate that required connection settings are present.

    Either a client certificate and key (mTLS) or a username and password must
    be configured, as for the SDK connection.
    """
    if not settings.get("connection_string"):
        raise ValueError("Missing required connection settings: connection_string")

    if bool(settings.get("client_cert_path")) != bool(settings.get("client_key_path")):
        raise ValueError(
            "Both client_cert_path and client_key_path must be provided together "
            "for certificate authentication; only one was set."
        )

    if uses_client_cert(settings):
        # The certificate is presented in the TLS handshake, so there is nothing
        # to authenticate with over a cleartext connection.
        if not str(settings["connection_string"]).lower().startswith("couchbases://"):
            raise ValueError(
                "Client certificate (mTLS) authentication requires a couchbases:// "
                "connection string."
            )
        return

    missing = [key for key in ("username", "password") if not settings.get(key)]
    if missing:
        raise ValueError(
            f"Missing required connection settings: {', '.join(missing)} "
            "(or set client_cert_path and client_key_path for certificate authentication)"
        )


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


def resolve_deployment(connection_string: str | None) -> Deployment | None:
    """Which deployment *connection_string* names, or ``None`` if it cannot tell.

    Recognition is by hostname, which makes the two answers unequally strong
    and that asymmetry is worth stating plainly:

    * ``CAPELLA`` is positive evidence — every host ends in
      ``.cloud.couchbase.com``, which nothing else does. Capella private
      endpoints keep that domain, so private-link clusters are recognised too.
    * ``ON_PREM`` is the absence of that evidence. A Capella cluster reached
      through a CNAME or a custom DNS name is read as on-prem here, because
      nothing in the connection string says otherwise.

    That second case is a real limitation, and it is *not* covered by the
    runtime guards inside the affected tools: those call
    ``is_capella_connection``, the same hostname test this resolver uses. A
    Capella alias misread here is therefore misread there as well — the tool
    registers, runs, and fails against an endpoint Capella does not expose,
    surfacing a connection or HTTP error rather than "not supported on
    Capella". The guards are a backstop for a host that resolved *no*
    deployment, not a second opinion on this one.

    Returns ``None`` when no host can be parsed out of the string, so an empty,
    malformed or host-less value withholds nothing rather than silently
    claiming one deployment or the other.
    """
    if not connection_string:
        return None
    try:
        hosts = extract_hosts_from_connection_string(connection_string)
    except ValueError:
        # ``urlparse`` rejects some malformed values outright — an unmatched
        # "[" raises "Invalid IPv6 URL" rather than returning empty parts.
        # Every other caller parses inside a tool call, where the failure
        # becomes an error response; this one runs during startup, so letting
        # it propagate would turn a typo in a connection string into a server
        # that does not boot, with a traceback pointing at tool gating rather
        # than at the typo. An unparseable string is simply a deployment we
        # cannot name; the connection attempt that follows reports it properly.
        logger.warning(
            "Could not parse connection string to determine deployment; "
            "no tools will be gated on it."
        )
        return None
    if not hosts:
        return None
    return (
        Deployment.CAPELLA
        if is_capella_connection(connection_string)
        else Deployment.ON_PREM
    )


def _get_capella_root_ca_path() -> str:
    """Get the path to the Capella root CA certificate.

    Uses importlib.resources to locate the certificate file, which works when the package is installed with fallback for development.

    Returns:
        Path to the Capella root CA certificate file.
    """
    try:
        # Use importlib.resources to get the certificate path (works for installed packages)
        cert_file = files("cb_mcp.utils.operational.certs").joinpath(
            "capella_root_ca.pem"
        )
        # Convert to string path - this works for both installed packages and dev mode
        return str(cert_file)
    except (ImportError, FileNotFoundError, TypeError):
        # Fallback for development: certs/ is a sibling of this file under utils/operational/.
        operational_dir = os.path.dirname(os.path.abspath(__file__))
        fallback_path = os.path.join(operational_dir, "certs", "capella_root_ca.pem")

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


def rest_client_auth(
    settings: Mapping[str, Any],
) -> tuple[bool | str | ssl.SSLContext, tuple[str, str] | None]:
    """The ``verify`` and ``auth`` values for an ``httpx.Client`` calling Couchbase REST.

    Authenticates the same way the SDK connection does. With a client
    certificate and key configured, the certificate is loaded into the TLS
    context (so it is presented in the handshake) and no basic-auth pair is
    returned; the cluster maps the certificate to a user. Otherwise the
    username/password pair is returned and ``verify`` is exactly what
    ``determine_ssl_verification`` says.

    Raises ``ValueError`` on settings ``validate_connection_settings`` rejects.
    """
    validate_connection_settings(settings)
    verify = determine_ssl_verification(
        settings["connection_string"], settings.get("ca_cert_path")
    )
    if not uses_client_cert(settings):
        return verify, (settings["username"], settings["password"])

    cert_path, key_path = settings["client_cert_path"], settings["client_key_path"]
    if not os.path.exists(cert_path) or not os.path.exists(key_path):
        raise ValueError(
            f"Client certificate files not found at {os.path.basename(cert_path)} "
            f"or {os.path.basename(key_path)}."
        )
    # httpx's own ``cert=`` argument and ``verify=<path>`` are deprecated; the
    # supported way to present a client certificate is a ready-made SSLContext.
    if isinstance(verify, str):
        context = ssl.create_default_context(cafile=verify)
    else:
        context = httpx.create_ssl_context(verify=verify)
    context.load_cert_chain(cert_path, key_path)
    return context, None
