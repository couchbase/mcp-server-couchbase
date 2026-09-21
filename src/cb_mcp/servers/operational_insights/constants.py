"""Identity constants for the Operational Insights server.

Separate from ``__init__`` so this server's tool and helper modules can
import its logger namespace cheaply. Note the separation alone is not what
avoids the import cycle — importing any submodule runs the package
``__init__`` first, so that file must also stay inert. See its docstring.

This module is deliberately SDK-free: ``src/mcp_server.py`` imports the
port/log-file values from here at module scope (for the ``transport_options``/
``logging_options`` factories), and importing the SDK there would defeat the
"a process only loads the SDK of the server it is actually running" rule —
the spec itself (which does import the SDK-touching tools) is imported
lazily, inside the subcommand body.
"""

from ...utils.constants import LOGGER_NAMESPACE

#: Stable id: the CLI subcommand, the telemetry dimension, and the value
#: reported as ``server_id``.
SERVER_ID = "operational-insights"

#: What MCP clients receive as ``serverInfo.name``. Wire-visible, so changing
#: it is a breaking change for every connected client.
FASTMCP_SERVER_NAME = "couchbase-operational-insights"

#: This server's logger namespace. Nested under the package namespace so the
#: handlers attached at the logging root still see these records, while
#: keeping them distinguishable from the operational server's in a merged
#: stream. The hyphen (matching SERVER_ID/fastmcp_name) is deliberate, not a
#: typo to "fix": ``logging`` splits hierarchy on "." only, so
#: "couchbase.mcp.operational-insights" is a *sibling* of
#: "couchbase.mcp.operational" rather than nesting under it — which is what
#: we want, since these are two independent servers.
OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE = f"{LOGGER_NAMESPACE}.{SERVER_ID}"

#: Default port for this server under a network transport. Must differ from
#: every other server's default_port (see ServerSpec.default_port).
DEFAULT_OI_PORT = 8001

#: Default base path for this server's log files. Must differ from every
#: other server's default_log_file — RotatingFileHandler is not
#: multi-process safe.
DEFAULT_OI_LOG_FILE = "mcp_server_operational_insights.log"
