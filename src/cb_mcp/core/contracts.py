"""
Host-agnostic contracts implemented by each MCP server host.

A *host* is a concrete MCP server — today either the standalone CLI in
this repo or the managed Capella runtime. Tool bodies live per-host, but
every host reaches its backing service through a provider, so the rest of
the machinery (lifespans, middleware, shared helpers) can be written
against one interface.

Two protocols, split by *who calls what*:

``ProviderLifecycle``
    Everything the shared machinery calls — teardown and status reporting.
    Service-agnostic, so this is what the shared layer annotates against
    (``AppContext.cluster_provider``, ``build_app``'s ``provider_factory``).

``ClusterProvider``
    ``ProviderLifecycle`` plus ``get_cluster`` returning a Couchbase
    ``Cluster``: the operational server's provider. Keeps its name and its
    home here because managed implementations already import it from this
    module, and its member set is unchanged — an implementation that
    satisfied the old single protocol satisfies this one.

A second service declares its own provider protocol next to that service's
helpers rather than here — see
``cb_mcp.utils.operational_insights.contracts.OperationalInsightsProvider``,
which adds ``handle_registry`` on top of a different ``get_cluster`` return
type. Keeping only the service-agnostic half in ``core`` is what lets this
module stay free of *both* SDKs.

The ``couchbase`` import below is deliberately under ``TYPE_CHECKING``: it
is needed only to annotate ``get_cluster``. Importing it at runtime pulled
~120 ``couchbase.*`` modules into every process that touched this module —
including the Operational Insights server, which defeated the "a process
loads only the SDK of the server it is actually running" rule that
``src/mcp_server.py``'s lazy subcommand imports exist to uphold.
``tests/unit/test_sdk_isolation.py`` guards this.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from fastmcp import Context

if TYPE_CHECKING:
    from couchbase.cluster import Cluster


class ProviderLifecycle(Protocol):
    """What the shared machinery calls on a provider, whatever it backs.

    Deliberately excludes ``get_cluster``. The cluster object travels
    straight from the provider to a tool body that already knows which SDK
    it is holding — no shared code ever touches it — so requiring a
    service-specific return type in the shared layer bought nothing and cost
    an SDK import.

    Annotating the shared layer against this is also what keeps teardown
    polymorphic: the operational client ends with ``close()`` and the
    Operational Insights client with ``shutdown()``, and each provider
    encapsulates that rather than the lifespan type-switching on the client.

    Not ``runtime_checkable``, deliberately. Such a protocol verifies method
    *names* only — never signatures or types — so unrelated providers
    sharing these names would all satisfy it, making ``isinstance`` against
    it actively misleading. Nothing branches on a provider's type: a
    server's spec already knows which one it built.
    """

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


class ClusterProvider(ProviderLifecycle, Protocol):
    """Resolves a Couchbase cluster for a given request.

    Implementations decide how credentials are sourced (static config,
    Secrets Manager, etc.) and how clusters are cached (one per server,
    one per principal, etc.).

    Only ``get_cluster`` is declared here; ``close`` / ``get_configuration``
    / ``is_connected`` are inherited from ``ProviderLifecycle``, so the
    protocol's shape is exactly what it was when all four were written out
    in a single class.
    """

    def get_cluster(self, ctx: Context) -> Cluster:
        """Return (or begin returning) a cluster for this request."""
        ...
