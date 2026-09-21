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
    DEFAULT_OI_LOG_FILE,
    DEFAULT_OI_PORT,
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
    tools=TOOL_SET,
    # Must differ from every other server's values (see ServerSpec.default_port
    # / default_log_file docstrings).
    default_port=DEFAULT_OI_PORT,
    default_log_file=DEFAULT_OI_LOG_FILE,
    # Reuses the canonical scope labels rather than declaring OI-specific
    # ones: this keeps auth.py, scope_enforcement.py and oauth_options
    # entirely unchanged. The tradeoff is that one token's scopes grant
    # across both servers — acceptable for now, revisit if per-service
    # scoping is ever needed.
    scopes=ScopeSpec(read=SCOPE_READ, write=SCOPE_WRITE),
    annotations=TOOL_ANNOTATIONS,
    scope_hints=TOOL_SCOPE_HINTS,
    # This server owns the couchbase_operational_insights SDK's logging.
    sdk_log_hook=bridge_sdk_logging,
    reported_dependencies=("couchbase-operational-insights",),
    # Every settings key this server's CLI produces (connection_string,
    # username, password, plus the server-agnostic block: read_only_mode,
    # transport, host, port, disabled_tools, confirmation_required_tools,
    # oauth_*) is already covered by the shared allow-lists in
    # utils/environment.py. Declaring any of them here would trip
    # test_spec_contributions_do_not_duplicate_the_shared_lists.
    safe_settings_keys=(),
    secret_settings_keys=(),
)
