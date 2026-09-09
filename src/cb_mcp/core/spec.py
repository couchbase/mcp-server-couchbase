"""Declarative description of an MCP server this package can build.

A *server* here is one coherent set of tools over one backing service —
today only the operational Couchbase cluster, but the shared machinery
(logging, OAuth, tool gating, telemetry, lifespan) is written against this
description rather than against the operational server directly, so a second
service can be added without touching any of it.

Why data rather than a base class: tools in this codebase are plain functions
with no decorators (registration happens in the host, see
``cb_mcp.core.app.build_app``), so a server has no behaviour to inherit — only
facts to declare. Keeping those facts in a frozen dataclass also makes the
cross-server invariants checkable without constructing a ``FastMCP`` instance,
opening a connection, or importing any SDK: a test can simply read the spec.

Deliberately *not* here: credentials and CLI options. Configuration parsing
belongs to the host (``src/mcp_server.py`` and ``src/providers/``), per the
architecture rule in CONTRIBUTING.md. A spec only declares which settings keys
are safe to log and which must be redacted, so the host cannot forget to
classify a new one.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from mcp.types import ToolAnnotations


@dataclass(frozen=True)
class ToolSet:
    """The tools one server exposes, split by whether they mutate state.

    ``read_only`` tools are always registered. ``write`` tools are registered
    only when the server is not in read-only mode — they are never loaded at
    all, rather than being loaded and refused at call time.

    Note that "read-only" here is a *registration-time* classification. Tools
    whose safety depends on their arguments (SQL++, which can carry a mutating
    statement) are read-only for registration purposes and enforce their own
    rules at runtime.
    """

    read_only: tuple[Callable, ...] = ()
    write: tuple[Callable, ...] = ()

    def tools_for(self, *, read_only_mode: bool) -> list[Callable]:
        """The tools to register under the given mode."""
        tools = list(self.read_only)
        if not read_only_mode:
            tools.extend(self.write)
        return tools

    @property
    def all_tools(self) -> list[Callable]:
        """Every tool this server can expose, regardless of mode."""
        return [*self.read_only, *self.write]

    @property
    def read_only_tool_names(self) -> frozenset[str]:
        return frozenset(fn.__name__ for fn in self.read_only)

    @property
    def write_tool_names(self) -> frozenset[str]:
        """Names requiring the write scope — the input to scope categorization."""
        return frozenset(fn.__name__ for fn in self.write)

    @property
    def all_tool_names(self) -> frozenset[str]:
        return self.read_only_tool_names | self.write_tool_names


@dataclass(frozen=True)
class ScopeSpec:
    """The canonical OAuth scope labels a server gates its tools with.

    These are the *canonical* values, the form token scopes are normalized to
    before per-tool enforcement. They are distinct from the operator-facing
    labels an IdP emits, which may differ and are mapped onto these — see
    ``cb_mcp.auth``. Code comparing against a token's scopes must use these,
    never the configured label.
    """

    read: str
    write: str


@dataclass(frozen=True)
class ServerSpec:
    """Everything the shared core needs to know to build one MCP server."""

    #: Stable internal identifier. Selects the server and tags telemetry.
    id: str

    #: The name passed to ``FastMCP(...)`` and reported by
    #: ``get_server_configuration_status``. Wire-visible: changing it is a
    #: breaking change for clients, independent of any logging concern.
    fastmcp_name: str

    #: Root of this server's logger hierarchy. Must be the configured logging
    #: root or a descendant of it, or ``configure_logging`` attaches handlers
    #: that never see these records.
    logger_namespace: str

    #: Human-readable name advertised in OAuth protected-resource metadata.
    display_name: str

    tools: ToolSet
    scopes: ScopeSpec

    #: Per-tool MCP annotations (readOnlyHint / destructiveHint / ...).
    annotations: Mapping[str, ToolAnnotations] = field(default_factory=dict)

    #: Per-tool explanations appended to a scope-denial error.
    scope_hints: Mapping[str, str] = field(default_factory=dict)

    #: The backing SDK's log-forwarding entry point, called as
    #: ``hook(logger_root, level)``. ``None`` means the SDK has no such hook,
    #: or must not be initialized in this server's process — several SDKs
    #: accept this call only once per process.
    sdk_log_hook: Callable[[str, int], None] | None = None

    #: Distribution names whose versions are worth reporting in the startup
    #: diagnostic snapshot, beyond the core dependencies every server shares.
    reported_dependencies: tuple[str, ...] = ()

    #: Settings keys safe to log verbatim in the diagnostic snapshot.
    safe_settings_keys: tuple[str, ...] = ()

    #: Settings keys whose *presence* may be logged but never their value.
    secret_settings_keys: tuple[str, ...] = ()
