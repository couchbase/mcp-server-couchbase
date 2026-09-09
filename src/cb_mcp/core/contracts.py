"""
Host-agnostic contracts implemented by each MCP server host.

A *host* is a concrete MCP server — today either the standalone CLI in
this repo or the managed Capella runtime. Tool bodies live per-host, but
both hosts reach a Couchbase cluster through the same
``ClusterProvider`` shape so that the rest of the machinery
(lifespans, middleware, shared helpers) can be written against a single
interface.
"""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from couchbase.cluster import Cluster
from fastmcp import Context


@runtime_checkable
class ClusterProvider(Protocol):
    """Resolves a Couchbase cluster for a given request.

    Implementations decide how credentials are sourced (static config,
    Secrets Manager, etc.) and how clusters are cached (one per server,
    one per principal, etc.).

    Note for a future second backing service (e.g. Enterprise Analytics,
    reached through the unrelated ``couchbase_analytics`` SDK): only
    ``get_cluster`` is Couchbase-specific. The shared machinery calls just
    ``close`` (lifespan teardown) and ``get_configuration`` / ``is_connected``
    (status reporting), so those three can be lifted into a common base
    protocol at that point, leaving each service its own ``get_cluster``
    return type. Doing so keeps teardown polymorphic — the operational client
    ends with ``close()`` and the analytics client with ``shutdown()``, and
    each provider should encapsulate that rather than the lifespan
    type-switching on the client. Deliberately not split yet: the right base
    is only knowable against a real second implementation.

    Beware when that happens: ``runtime_checkable`` verifies method *names*
    only, never signatures or types, so unrelated providers sharing these
    names all satisfy this protocol. Never branch on ``isinstance`` against
    it — a server's spec already knows its provider type.
    """

    def get_cluster(self, ctx: Context) -> Cluster:
        """Return (or begin returning) a cluster for this request."""
        ...

    def close(self) -> None:
        """Release any clusters held by this provider and perform cleanup."""
        ...

    def get_configuration(self, ctx: Context) -> Mapping[str, Any]:
        """Provider-specific configuration suitable for status reporting.

        Must not include secrets — return ``_configured`` booleans instead.
        Returned keys are merged into the top-level ``configuration`` dict of
        ``get_server_configuration_status``; implementations must not reuse
        server-level key names (``read_only_mode``, ``disabled_tools``,
        ``confirmation_required_tools``) since those are
        owned by the server and would silently override any provider value.
        Implementations may use ``ctx`` to return per-caller configuration
        (e.g., per-API-key in managed implementations) or ignore it (static implementations).
        """
        ...

    def is_connected(self, ctx: Context) -> bool:
        """True if a cluster is currently open for this caller.

        Implementations may use ``ctx`` to check per-caller connection state
        (e.g., per-principal cache entry) or ignore it (static implementations).
        """
        ...
