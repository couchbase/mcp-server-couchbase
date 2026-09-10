"""Environment introspection for diagnostic logging.

Emits a single DEBUG record on server start summarising the OS, Python
runtime, MCP server version, key dependency versions, transport, effective
log level, and a redacted view of the server configuration.

Intended audience: customer support. When a user reports an issue, asking
them to enable DEBUG logging will produce this record with most of the
context needed to triage — no further back-and-forth required.

The config redaction mirrors the policy of the ``get_server_configuration_status``
MCP tool (see :mod:`cb_mcp.tools.operational.server`) and the provider's
``get_configuration`` so that the log file and the MCP tool output agree on
what's safe to expose. Secrets (passwords, certificate file paths) are
replaced with ``*_configured`` booleans; identifiers the user typed into
their config (connection_string) are logged verbatim.
"""

import json
import logging
import platform
import sys
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from .constants import LOGGER_NAMESPACE
from .logging import get_resolved_logging_config

if TYPE_CHECKING:  # avoid a runtime import cycle: core.spec is unrelated to logging
    from ..core.spec import ServerSpec

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.utils.environment")

# Dependencies every server shares. A server's own backing-SDK packages come
# from its spec (``reported_dependencies``) and are appended to these, so this
# module never has to know which SDKs exist. Keep both lists small to avoid log
# spam; add entries only when knowing the pinned version would meaningfully
# change how a ticket is investigated.
_CORE_DEPENDENCIES = ("fastmcp", "mcp", "httpx", "click")

# Settings keys whose full values are safe to include in the diagnostic
# record. Anything not in this set or _PRESENCE_ONLY_KEYS is dropped —
# allow-list is the right default for a log line that may end up in a
# customer-shared support bundle.
_SAFE_SETTINGS_KEYS = (
    "read_only_mode",
    "transport",
    "host",
    "port",
    "disabled_tools",
    "confirmation_required_tools",
    "connection_string",
    # An identifier, not a credential. Reported verbatim to match
    # get_server_configuration_status, which already returns it to any
    # connected MCP client via the provider — withholding it here only made
    # the local support bundle less useful than the wire response.
    "username",
    # OAuth resource-server config: non-secret IdP coordinates (JWKS URL,
    # issuer, audience, algorithm, PRM base URL, effective scope labels) plus
    # an oauth_enabled flag. There is no client secret to redact — the server
    # only validates JWTs against a public JWKS — so these are safe to log
    # verbatim.
    "oauth_enabled",
    "oauth_jwks_uri",
    "oauth_issuer",
    "oauth_audience",
    "oauth_algorithm",
    "oauth_mcp_base_url",
    "oauth_scope_read_label",
    "oauth_scope_write_label",
)

# Settings whose presence is diagnostically useful but whose values are
# secrets or filesystem paths. Logged as ``<key>_configured: true/false``,
# matching the naming convention used by the provider's get_configuration.
# Credential keys that differ per server come from
# ``ServerSpec.secret_settings_keys``; these are the ones every server has.
_PRESENCE_ONLY_KEYS = (
    "password",
    "ca_cert_path",
)


def _package_version(package_name: str) -> str:
    """Return the installed version of a package, or 'unknown' if missing."""
    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unknown"


def safe_keys_for(spec: "ServerSpec | None" = None) -> tuple[str, ...]:
    """Keys logged verbatim: the shared set plus the server's own."""
    return _SAFE_SETTINGS_KEYS + (tuple(spec.safe_settings_keys) if spec else ())


def presence_only_keys_for(spec: "ServerSpec | None" = None) -> tuple[str, ...]:
    """Keys logged as ``<key>_configured`` booleans, never by value."""
    return _PRESENCE_ONLY_KEYS + (tuple(spec.secret_settings_keys) if spec else ())


def _redacted_settings(
    server_settings: Mapping[str, Any], spec: "ServerSpec | None" = None
) -> dict[str, Any]:
    """Project ``server_settings`` onto the safe-to-log subset.

    Safe keys are emitted as-is; secret keys are emitted as ``<key>_configured``
    booleans. Any other key is **dropped silently** — an allow-list is the right
    default for a record that may end up in a customer-shared support bundle,
    but it means an unclassified key vanishes without warning. See
    ``test_every_settings_key_is_classified`` for the guard against that.
    """
    redacted: dict[str, Any] = {}
    for key in safe_keys_for(spec):
        value = server_settings.get(key)
        # Normalise iterables of tool names so the log is stable across runs.
        if isinstance(value, set | frozenset | list | tuple):
            redacted[key] = sorted(value)
        else:
            redacted[key] = value
    for key in presence_only_keys_for(spec):
        redacted[f"{key}_configured"] = bool(server_settings.get(key))
    return redacted


def log_environment_info(
    transport: str,
    server_settings: Mapping[str, Any],
    spec: "ServerSpec | None" = None,
) -> None:
    """Emit one DEBUG record describing the runtime environment.

    The payload is emitted as a JSON-encoded object after an ``Environment |``
    prefix. The prefix keeps the record greppable in plain-text logs; the JSON
    body lets log aggregators and support tooling parse individual fields
    without regex gymnastics.

    The ``logging`` block mirrors what the ``get_server_configuration_status``
    MCP tool returns, so support engineers reading the log and tools reading
    the MCP response see the same shape and field names.

    The record is written two ways. First, as pure JSON to a dedicated
    non-rotating file in overwrite mode, so the current
    server config is captured even at INFO (not only DEBUG) and survives
    rotation of the debug file. This dedicated file is only written when
    file-based logging is enabled (the ``file`` sink is active). Second, it is emitted as a
    DEBUG log record (with the ``Environment |`` prefix) for live/stderr
    visibility.
    """
    resolved_logging = get_resolved_logging_config()
    info: dict[str, Any] = {
        "os": platform.platform(),
        "platform": sys.platform,
        "arch": platform.machine(),
        "python": platform.python_version(),
        "mcp_server_version": _package_version("couchbase-mcp-server"),
        "dependencies": {
            name: _package_version(name)
            for name in (
                _CORE_DEPENDENCIES + (tuple(spec.reported_dependencies) if spec else ())
            )
        },
        "transport": transport,
        "server_id": spec.id if spec else None,
        # The name clients see. Recorded here because FastMCP logs it only
        # to stderr, so a file-only log bundle would otherwise never state it.
        "server_name": spec.fastmcp_name if spec else None,
        "logging": resolved_logging.as_dict() if resolved_logging else None,
        "config": _redacted_settings(server_settings, spec),
    }
    payload = json.dumps(info, default=str)

    # Durable copy: overwrite the dedicated JSON file so it always holds the
    # current run's snapshot, independent of log level and immune to rotation of
    # the debug file. Only present when the file sink is active.
    server_config_file = (
        resolved_logging.server_config_file if resolved_logging else None
    )
    if server_config_file:
        try:
            with open(server_config_file, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")
        except OSError as e:
            logger.error(
                "Could not write server config file %r: %s", server_config_file, e
            )

    # Live/stderr visibility at DEBUG (filtered by the logger's effective level).
    logger.debug("Environment | %s", payload)
