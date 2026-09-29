"""Every ``ServerSpec`` this distribution ships. Test-only by design.

``src/`` deliberately has no such registry: ``src/mcp_server.py`` imports each
server's spec lazily, inside its own subcommand body, so a process loads only
the SDK of the server it is actually running (see CONTRIBUTING.md's "Adding a
new MCP server" step 5). Importing this module pulls in *both* backing
SDKs — harmless for a test process, but exactly the coupling that rule exists
to avoid in production, which is why this lives in ``tests/`` and not
``src/cb_mcp``.

Importable from any test subdirectory via ``pythonpath = ["tests"]`` in
``pyproject.toml``, the same mechanism ``tests/_test_env.py`` uses.
"""

from cb_mcp.servers.operational.spec import SPEC as OPERATIONAL_SPEC
from cb_mcp.servers.operational_insights.spec import SPEC as OPERATIONAL_INSIGHTS_SPEC

ALL_SPECS = (OPERATIONAL_SPEC, OPERATIONAL_INSIGHTS_SPEC)
SPEC_BY_ID = {spec.id: spec for spec in ALL_SPECS}
