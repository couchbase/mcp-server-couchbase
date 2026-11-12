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
DEFAULT_OPERATIONAL_INSIGHTS_PORT = 8001

#: Default base path for this server's log files. Must differ from every
#: other server's default_log_file — RotatingFileHandler is not
#: multi-process safe.
DEFAULT_OPERATIONAL_INSIGHTS_LOG_FILE = "mcp_server_operational_insights.log"

#: Default for --operational-insights-async-query-result-timeout, in seconds.
#: The SDK's own default (10s) is a deadline on the whole of
#: ``fetch_results()``, row stream included, so a result that takes longer
#: than that to download can never be fetched. See
#: ``utils/operational_insights/connection.py``.
DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT = 30

#: Upper bound for --operational-insights-async-query-result-timeout, in
#: seconds. A larger value is clamped to this with a warning. Every async
#: handle request shares the timeout, so an unbounded value would also let a
#: status, discard or cancel call hang that long on a server that has stopped
#: answering.
MAX_ASYNC_QUERY_RESULT_TIMEOUT = 600

#: Retry policy for the REST-backed async tools (``*_rest``). Same retry
#: *rules* as the SDK's ``RetryHandler`` (connect failures, HTTP 503, and
#: errors the server marks ``retriable``), with tighter limits: the SDK allows
#: 7 retries and waits of up to 60s, which can hold one tool call open for
#: minutes while a model waits on it.
REST_MAX_RETRIES = 3

#: Backoff bounds, in seconds. Each wait is drawn uniformly from
#: ``[0, min(REST_BACKOFF_MAX_SECONDS, REST_BACKOFF_MIN_SECONDS * 2**(attempt-1))]``
#: — the SDK's full-jitter exponential shape.
REST_BACKOFF_MIN_SECONDS = 0.1
REST_BACKOFF_MAX_SECONDS = 2.0

#: Ceiling on the total time one call may spend sleeping between retries.
REST_RETRY_BUDGET_SECONDS = 10.0

#: Server-side timeout sent with every REST-started query. Matches what the
#: SDK sends by default: its 10-minute query timeout plus the 5s it adds so
#: the client side always fires first.
REST_QUERY_TIMEOUT = "605000.0ms"
