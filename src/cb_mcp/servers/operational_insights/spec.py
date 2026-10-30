"""The Operational Insights server.

Its identity is declared here as data so the shared core can build it
without special-casing it, and so cross-server invariants can be checked by
reading the spec rather than starting a server.
"""

from ...core.spec import ScopeSpec, ServerSpec
from ...tools.operational_insights import TOOL_ANNOTATIONS, TOOL_SCOPE_HINTS, TOOL_SET
from ...utils.constants import SCOPE_READ, SCOPE_WRITE
from ...utils.operational_insights.sdk_logging import bridge_sdk_logging
from .constants import (
    DEFAULT_OPERATIONAL_INSIGHTS_LOG_FILE,
    DEFAULT_OPERATIONAL_INSIGHTS_PORT,
    FASTMCP_SERVER_NAME,
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
    SERVER_ID,
)

SPEC = ServerSpec(
    id=SERVER_ID,
    # Wire-visible: what clients receive as serverInfo.name.
    fastmcp_name=FASTMCP_SERVER_NAME,
    logger_namespace=OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
    display_name="Couchbase Operational Insights MCP Server",
    # Audited under its own Tier-2 block (0x10000). It shares the category axis
    # with the operational server but not the block: the two collide on tool
    # names — get_server_configuration_status, create_index, list_indexes,
    # get_collections_in_scope, get_schema_for_collection — and booking this
    # server's calls against the operational block would file them under ids
    # that mean a different server.
    audit_package="operational_insights",
    tools=TOOL_SET,
    # Must differ from every other server's values (see ServerSpec.default_port
    # / default_log_file docstrings).
    default_port=DEFAULT_OPERATIONAL_INSIGHTS_PORT,
    default_log_file=DEFAULT_OPERATIONAL_INSIGHTS_LOG_FILE,
    # Reuses the canonical scope labels rather than declaring Operational Insights-specific
    # ones: this keeps auth.py, scope_enforcement.py and oauth_options
    # entirely unchanged. The tradeoff is that one token's scopes grant
    # across both servers — acceptable for now, revisit if per-service
    # scoping is ever needed.
    scopes=ScopeSpec(read=SCOPE_READ, write=SCOPE_WRITE),
    annotations=TOOL_ANNOTATIONS,
    scope_hints=TOOL_SCOPE_HINTS,
    # Async query handles live in one process's QueryResultsRegistry (see
    # utils/operational_insights/handle_registry.py), so a token minted on
    # one worker is unknown to the others. Single-process stateless HTTP is
    # fine: the registry is per-process, not per-session.
    supports_multiple_workers=False,
    # This server owns the couchbase_operational_insights SDK's logging.
    sdk_log_hook=bridge_sdk_logging,
    reported_dependencies=("couchbase-operational-insights",),
    # connection_string, username, password, ca_cert_path and the
    # server-agnostic block (read_only_mode, transport, host, port,
    # disabled_tools, confirmation_required_tools, oauth_*) are already
    # covered by the shared allow-lists in utils/environment.py. Declaring any
    # of them here would trip
    # test_spec_contributions_do_not_duplicate_the_shared_lists.
    # client_cert_path/client_key_path/client_cert_password are this server's
    # mTLS client-certificate material and have no shared-list equivalent
    # (the operational server declares its own two-key subset the same way).
    # The async query result timeout is a plain number of seconds, safe to
    # log verbatim.
    safe_settings_keys=("operational_insights_async_query_result_timeout",),
    secret_settings_keys=(
        "client_cert_path",
        "client_key_path",
        "client_cert_password",
    ),
)
