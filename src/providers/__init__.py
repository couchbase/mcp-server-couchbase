"""Standalone-host provider implementations, one per server.

Each satisfies the ``ProviderLifecycle`` half of the contract that the
shared machinery calls, plus its own service's provider protocol:
``operational.py`` -> ``cb_mcp.core.contracts.ClusterProvider``,
``operational_insights.py`` ->
``cb_mcp.utils.operational_insights.contracts.OperationalInsightsProvider``.

Named for the server each serves, not for how it sources credentials:
``operational.py`` was ``static.py`` (``StaticClusterProvider``) back when
it was the only one, which left the two modules describing themselves along
different axes once a second arrived — and "static" was never the
distinguishing fact anyway, since both hold one cluster for the life of the
server.

Kept inert (no re-exports) so that importing one provider module never pulls
in another server's SDK: ``operational.py`` imports the ``couchbase`` SDK,
``operational_insights.py`` imports ``couchbase_operational_insights``.
``tests/unit/test_sdk_isolation.py`` holds that line. Importers should reach
directly into the submodule they need — e.g. ``from providers.operational
import OperationalClusterProvider`` — the same "import the spec/provider
lazily inside the subcommand" rule ``src/mcp_server.py`` follows for server
specs.
"""
