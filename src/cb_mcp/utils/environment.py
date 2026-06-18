"""Environment introspection for diagnostic logging.

Emits a single DEBUG record on server start summarising the OS, Python
runtime, MCP server version, key dependency versions, transport, effective
log level, and a redacted view of the server configuration.

Intended audience: customer support. When a user reports an issue, asking
them to enable DEBUG logging will produce this record with most of the
context needed to triage — no further back-and-forth required.

The config redaction mirrors the policy of the ``get_server_configuration_status``
MCP tool (see :mod:`cb_mcp.tools.server`) and the provider's
``get_configuration`` so that the log file and the MCP tool output agree on
what's safe to expose. Secrets (passwords, certificate file paths) are
replaced with ``*_configured`` booleans; identifiers the user typed into
their config (connection_string, username) are logged verbatim.
"""

import json
import logging
import platform
import sys
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from .constants import MCP_SERVER_NAME
from .logging import get_resolved_logging_config

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.environment")

# Key transitive dependencies whose versions are useful for support triage.
# Keep this list small to avoid log spam; add entries only when knowing the
# pinned version would meaningfully change how a ticket is investigated.
_REPORTED_DEPENDENCIES = ("fastmcp", "mcp", "couchbase", "httpx", "click", "lark")

# Settings keys whose full values are safe to include in the diagnostic
# record. Anything not in this set or _PRESENCE_ONLY_KEYS is dropped —
# allow-list is the right default for a log line that may end up in a
# customer-shared support bundle.
_SAFE_SETTINGS_KEYS = (
    "read_only_mode",
    "read_only_query_mode",
    "transport",
    "host",
    "port",
    "disabled_tools",
    "confirmation_required_tools",
    "connection_string",
    "username",
    # OAuth resource-server config: non-secret IdP coordinates (JWKS URL,
    # issuer, audience, algorithm, PRM base URL) plus an oauth_enabled flag.
    # There is no client secret to redact — the server only validates JWTs
    # against a public JWKS — so these are safe to log verbatim.
    "oauth_enabled",
    "oauth_jwks_uri",
    "oauth_issuer",
    "oauth_audience",
    "oauth_algorithm",
    "oauth_mcp_base_url",
)

# Settings whose presence is diagnostically useful but whose values are
# secrets or filesystem paths. Logged as ``<key>_configured: true/false``,
# matching the naming convention used by the provider's get_configuration.
_PRESENCE_ONLY_KEYS = (
    "password",
    "ca_cert_path",
    "client_cert_path",
    "client_key_path",
)


def _package_version(package_name: str) -> str:
    """Return the installed version of a package, or 'unknown' if missing."""
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unknown"


def _redacted_settings(server_settings: Mapping[str, Any]) -> dict[str, Any]:
    """Project ``server_settings`` onto the safe-to-log subset.

    Keys in ``_SAFE_SETTINGS_KEYS`` are emitted as-is; keys in
    ``_PRESENCE_ONLY_KEYS`` are emitted as ``<key>_configured`` booleans.
    Any other key is dropped.
    """
    redacted: dict[str, Any] = {}
    for key in _SAFE_SETTINGS_KEYS:
        value = server_settings.get(key)
        # Normalise iterables of tool names so the log is stable across runs.
        if isinstance(value, set | frozenset | list | tuple):
            redacted[key] = sorted(value)
        else:
            redacted[key] = value
    for key in _PRESENCE_ONLY_KEYS:
        redacted[f"{key}_configured"] = bool(server_settings.get(key))
    return redacted


def log_environment_info(transport: str, server_settings: Mapping[str, Any]) -> None:
    """Emit one DEBUG record describing the runtime environment.

    The payload is emitted as a JSON-encoded object after an ``Environment |``
    prefix. The prefix keeps the record greppable in plain-text logs; the JSON
    body lets log aggregators and support tooling parse individual fields
    without regex gymnastics.

    The ``logging`` block mirrors what the ``get_server_configuration_status``
    MCP tool returns, so support engineers reading the log and tools reading
    the MCP response see the same shape and field names.

    Fires unconditionally; the record is filtered by the logger's effective
    level. Customers running at INFO see nothing; enabling DEBUG surfaces the
    full diagnostic line without any code change.
    """
    resolved_logging = get_resolved_logging_config()
    info: dict[str, Any] = {
        "os": platform.platform(),
        "platform": sys.platform,
        "arch": platform.machine(),
        "python": platform.python_version(),
        "mcp_server_version": _package_version("couchbase-mcp-server"),
        "dependencies": {
            name: _package_version(name) for name in _REPORTED_DEPENDENCIES
        },
        "transport": transport,
        "logging": resolved_logging.as_dict() if resolved_logging else None,
        "config": _redacted_settings(server_settings),
    }
    logger.debug("Environment | %s", json.dumps(info, default=str))
