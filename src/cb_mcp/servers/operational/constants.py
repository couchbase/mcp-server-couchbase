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

# Cluster major version at which list_indexes prefers the query service over
# the Index Service REST API. From this version, system:indexes exposes the
# original CREATE INDEX statement in metadata.definition, so we query it
# instead of the /getIndexStatus REST endpoint. Couchbase-specific, so it
# lives with this server rather than in the shared constants.
QUERY_SERVICE_LIST_INDEXES_MIN_MAJOR_VERSION = 8
