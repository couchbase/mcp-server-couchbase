"""Standalone-host provider implementations, one per server.

Each satisfies the ``ProviderLifecycle`` half of the contract that the
shared machinery calls, plus its own service's provider protocol:
``static.py`` -> ``cb_mcp.core.contracts.ClusterProvider``,
``operational_insights.py`` ->
``cb_mcp.utils.operational_insights.contracts.OperationalInsightsProvider``.

Kept inert (no re-exports) so that importing one provider module never pulls
in another server's SDK. ``static.py`` imports the ``couchbase`` SDK;
``operational_insights.py`` imports ``couchbase_operational_insights``.
``tests/unit/test_sdk_isolation.py`` holds that line.
Importers should reach directly into the submodule they need — e.g.
``from providers.static import StaticClusterProvider`` — the same "import
the spec/provider lazily inside the subcommand" rule ``src/mcp_server.py``
follows for server specs.
"""
