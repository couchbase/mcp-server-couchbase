"""
Enterprise Analytics (EA) tools.

READ THIS BEFORE ADDING A TOOL HERE.

This file is exclusively for Couchbase **Enterprise Analytics** — a separate
product from the operational Data/Query/Index services covered by
`kv.py`/`query.py`/`index.py`/`collection_management.py`. Do not mix EA and
operational logic in the same tool, and do not assume anything from those
files carries over here; very little does.

1. Different SDK, different cluster object.
   EA is reached through the **separate `couchbase_analytics` pip package**,
   not `couchbase`. When `connection_mode == "analytics"`,
   `get_cluster_connection(ctx)` (from `..utils.context`) returns a
   `couchbase_analytics.cluster.Cluster` — a distinct, unrelated class from
   the operational `couchbase.cluster.Cluster`. It has no `.bucket()`,
   `.query()`, or `.collection()`. Query it with `cluster.execute_query(...)`
   and consume results via `.get_all_rows()` (confirmed against the
   installed `couchbase-analytics` SDK). See
   `..utils.connection.connect_to_analytics_cluster` for how the connection
   itself is constructed (it also needs `SecurityOptions(trust_only_capella=
   False, ...)` — the SDK defaults to trusting only Capella's CA, which
   would fail verification against every self-managed cluster).

2. Self-managed only, enforced upstream.
   EA support only covers self-managed clusters today (Capella's EA offering
   has known load-balancer issues). This is enforced once, at server
   startup, in `mcp_server.py`'s `connection_mode` validation — if you're
   writing a tool here, you can assume the active cluster is never Capella.
   Don't add a redundant per-tool Capella check without first revisiting
   that decision; see the connection-infra plan for the reasoning.

3. Tool registration is exactly like the operational families.
   Add read tools to `ANALYTICS_READ_ONLY_TOOLS` and write tools to
   `ANALYTICS_WRITE_TOOLS` in `tools/__init__.py` (both currently empty
   placeholders — no EA tools exist yet). `get_tools()` already loads
   whichever family matches `connection_mode`, and `tool_registration.py`'s
   `write_tool_names` union already includes `ANALYTICS_WRITE_TOOLS` (it's a
   no-op today since the list is empty) — so the first EA write tool you add
   automatically gets classified as requiring `couchbase-mcp:write` for
   OAuth callers with no extra step.

4. Known naming collision to watch for.
   The ticket's proposed EA tool `get_schema_for_collection` collides with
   an existing *operational* tool of the same name in `query.py`. Since
   `get_tools()` only ever loads one family at a time this isn't a runtime
   conflict, but it will be confusing in docs/tests/READMEs that list both
   families together — flag it when tool naming is finalized rather than
   silently reusing the identical name.

5. Scope not yet implemented.
   The ticket's P0 tools (get_databases_in_cluster, get_scopes_in_database,
   get_collections_in_scope, get_schema_for_collection, run_query_sync) are
   intentionally deferred to a follow-up change, once tool naming/scope are
   finalized. This file currently only exists so that connection/config
   infra (connection_mode, connect_to_analytics_cluster, tool-list gating)
   has a real home to point at.
"""
