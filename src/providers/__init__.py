"""Standalone-host ``ClusterProvider`` implementations.

Kept inert (no re-exports) so that importing one provider module never pulls
in another server's SDK. ``static.py`` imports the ``couchbase`` SDK;
``operational_insights.py`` imports ``couchbase_operational_insights``.
Importers should reach directly into the submodule they need — e.g.
``from providers.static import StaticClusterProvider`` — the same "import
the spec/provider lazily inside the subcommand" rule ``src/mcp_server.py``
follows for server specs.
"""
