"""The operational Couchbase cluster server.

This is the server that has always shipped: Data, Query and Index services
reached through the ``couchbase`` SDK. Its identity is declared here as data so
the shared core can build it without special-casing it, and so cross-server
invariants can be checked by reading the spec rather than starting a server.

Every value below is the one already in use — this module names existing
behaviour, it does not change it.
"""

import couchbase

from ..core.spec import ScopeSpec, ServerSpec
from ..tools import TOOL_ANNOTATIONS, TOOL_SET
from ..utils.constants import (
    FASTMCP_SERVER_NAME,
    LOGGER_ROOT,
    SCOPE_READ,
    SCOPE_WRITE,
)
from ..utils.scope_enforcement import TOOL_SCOPE_HINTS

SERVER_ID = "operational"

SPEC = ServerSpec(
    id=SERVER_ID,
    # Wire-visible; must stay "couchbase" for already-connected clients.
    fastmcp_name=FASTMCP_SERVER_NAME,
    # This server owns the bare logging root: its loggers are the ones
    # operators and support runbooks already grep for, so it nests nothing.
    logger_namespace=LOGGER_ROOT,
    display_name="Couchbase MCP Server",
    tools=TOOL_SET,
    scopes=ScopeSpec(read=SCOPE_READ, write=SCOPE_WRITE),
    annotations=TOOL_ANNOTATIONS,
    scope_hints=TOOL_SCOPE_HINTS,
    # This server owns the Couchbase SDK's logging. The SDK accepts this call
    # only once per process, so no other server may make it.
    sdk_log_hook=couchbase.configure_logging,
    reported_dependencies=("couchbase", "lark"),
    safe_settings_keys=("connection_string",),
    secret_settings_keys=(
        "username",
        "password",
        "ca_cert_path",
        "client_cert_path",
        "client_key_path",
    ),
)
