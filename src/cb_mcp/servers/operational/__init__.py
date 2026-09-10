"""The operational Couchbase cluster server.

This is the server that has always shipped: Data, Query and Index services
reached through the ``couchbase`` SDK. Its identity is declared here as data so
the shared core can build it without special-casing it, and so cross-server
invariants can be checked by reading the spec rather than starting a server.

Every value below is the one already in use — this module names existing
behaviour, it does not change it.
"""

import couchbase

from ...core.spec import ScopeSpec, ServerSpec
from ...tools.operational import TOOL_ANNOTATIONS, TOOL_SET
from ...utils.constants import (
    DEFAULT_LOG_FILE,
    DEFAULT_PORT,
    FASTMCP_SERVER_NAME,
    LOGGER_ROOT,
    SCOPE_READ,
    SCOPE_WRITE,
)
from ...utils.scope_enforcement import TOOL_SCOPE_HINTS

SERVER_ID = "operational"


def _configure_couchbase_sdk_logging(logger_root: str, level: int) -> None:
    """Route the Couchbase SDK's own records into ``logger_root``.

    Wrapped rather than referencing ``couchbase.configure_logging`` directly in
    the spec, so the attribute is looked up per call. Binding the function
    object at import time would freeze it before tests could patch it — and
    the SDK accepts this call only once per process, so tests must be able to.
    """
    couchbase.configure_logging(logger_root, level)


SPEC = ServerSpec(
    id=SERVER_ID,
    # Wire-visible; must stay "couchbase" for already-connected clients.
    fastmcp_name=FASTMCP_SERVER_NAME,
    # This server owns the bare logging root: its loggers are the ones
    # operators and support runbooks already grep for, so it nests nothing.
    logger_namespace=LOGGER_ROOT,
    display_name="Couchbase MCP Server",
    tools=TOOL_SET,
    # The long-standing values: this server keeps them so existing
    # deployments, compose files and log tooling are unaffected. A second
    # server must pick different ones.
    default_port=DEFAULT_PORT,
    default_log_file=DEFAULT_LOG_FILE,
    scopes=ScopeSpec(read=SCOPE_READ, write=SCOPE_WRITE),
    annotations=TOOL_ANNOTATIONS,
    scope_hints=TOOL_SCOPE_HINTS,
    # This server owns the Couchbase SDK's logging. The SDK accepts this call
    # only once per process, so no other server may make it.
    sdk_log_hook=_configure_couchbase_sdk_logging,
    reported_dependencies=("couchbase", "lark"),
    # Only what is genuinely operational-specific: the shared env-info lists
    # already cover connection_string, password and ca_cert_path, which every
    # server has. mTLS client credentials have no analytics equivalent.
    safe_settings_keys=(),
    secret_settings_keys=("client_cert_path", "client_key_path"),
)
