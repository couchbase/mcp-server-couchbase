"""Identity constants for the operational Couchbase server.

Separate from ``__init__`` so this server's tool and helper modules can
import its logger namespace cheaply. Note the separation alone is not what
avoids the import cycle — importing any submodule runs the package
``__init__`` first, so that file must also stay inert. See its docstring.
"""

from ...utils.constants import LOGGER_NAMESPACE

#: Stable id: the CLI subcommand, the telemetry dimension, and the value
#: reported as ``server_id``.
SERVER_ID = "operational"

#: What MCP clients receive as ``serverInfo.name``. Wire-visible, so changing
#: it is a breaking change for every connected client.
FASTMCP_SERVER_NAME = "couchbase-operational"

#: This server's logger namespace. Nested under the package namespace so the
#: handlers attached at the logging root still see these records, while
#: keeping them distinguishable from another server's in a merged stream.
OPERATIONAL_LOGGER_NAMESPACE = f"{LOGGER_NAMESPACE}.{SERVER_ID}"

#: Default port for this server under a network transport. Must differ from
#: every other server's default_port (see ServerSpec.default_port).
#:
#: The long-standing value, kept so existing deployments and compose files
#: are unaffected. It lived in ``utils/constants`` as a bare ``DEFAULT_PORT``
#: until a second server existed; ``mcp_server.py`` then had to import it
#: ``as OPERATIONAL_DEFAULT_PORT`` to say at the call site what the shared
#: name no longer could. Naming it here removes the alias and puts it where
#: its Operational Insights counterpart already lived.
DEFAULT_OPERATIONAL_PORT = 8000

#: Default base path for this server's log files. Must differ from every
#: other server's default_log_file — RotatingFileHandler is not
#: multi-process safe. Same history as the port above.
#:
#: Distinct from ``utils.constants.FALLBACK_LOG_FILE``, which happens to
#: share the value: that one is what the logging module writes to when a
#: host enables file logging without configuring a path at all, and belongs
#: to no server.
DEFAULT_OPERATIONAL_LOG_FILE = "mcp_server.log"

# Cluster major version at which list_indexes prefers the query service over
# the Index Service REST API. From this version, system:indexes exposes the
# original CREATE INDEX statement in metadata.definition, so we query it
# instead of the /getIndexStatus REST endpoint. Couchbase-specific, so it
# lives with this server rather than in the shared constants.
QUERY_SERVICE_LIST_INDEXES_MIN_MAJOR_VERSION = 8
