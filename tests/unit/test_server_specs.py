"""Cross-server invariants declared by ``core/spec.py`` but never checked.

``ServerSpec``'s docstrings document several rules that only matter once a
second server exists: ports and log files must differ, namespaces must nest
under the package root, tool names should be unique, and so on. Before the
Operational Insights server, there was nothing to check these against. This
file is that check, over every spec in ``tests/_all_specs.ALL_SPECS``.
"""

from _all_specs import ALL_SPECS

from cb_mcp.utils.constants import LOGGER_NAMESPACE

# A name appearing on two servers means one of two very different things,
# and the tests below keep them apart rather than lumping both into one
# allow-list.
#
# SHARED: one function object, registered by every server. Not a collision
# at all — a client connected to both sees one tool that behaves identically
# whichever it reaches, which is the point. Enforced, not just declared:
# test_shared_tools_are_literally_the_same_function fails if two servers
# ever grow separate implementations under a shared name.
SHARED_TOOL_NAMES = frozenset({"get_server_configuration_status"})

# KNOWN_DUPLICATE: same name, *different* implementations per server. A real
# collision, grandfathered. Per CONTRIBUTING.md's tool-naming section: each
# server runs as an independent process, so this only matters to a client
# that registers both simultaneously. Renaming was considered and declined —
# these are the ported prototype's original names. This allow-list exists so
# a *new*, unintended collision still fails the build; it does not silence
# these.
KNOWN_DUPLICATE_TOOL_NAMES = frozenset(
    {
        "get_collections_in_scope",
        "get_schema_for_collection",
        "create_index",
        "list_indexes",
    }
)

#: Both kinds are permitted to appear on more than one server; only the
#: reason differs.
_EXPECTED_MULTI_SERVER_NAMES = SHARED_TOOL_NAMES | KNOWN_DUPLICATE_TOOL_NAMES


def test_ids_are_unique():
    ids = [spec.id for spec in ALL_SPECS]
    assert len(ids) == len(set(ids)), f"duplicate spec.id values: {ids}"


def test_fastmcp_names_are_unique():
    """Wire-visible; two servers sharing serverInfo.name would be indistinguishable."""
    names = [spec.fastmcp_name for spec in ALL_SPECS]
    assert len(names) == len(set(names)), f"duplicate fastmcp_name values: {names}"


def test_display_names_are_unique():
    """Shown in OAuth protected-resource metadata."""
    names = [spec.display_name for spec in ALL_SPECS]
    assert len(names) == len(set(names)), f"duplicate display_name values: {names}"


def test_logger_namespaces_are_unique_and_disjoint():
    """Two servers logging into the same hierarchy must stay distinguishable."""
    namespaces = [spec.logger_namespace for spec in ALL_SPECS]
    assert len(namespaces) == len(set(namespaces)), (
        f"duplicate logger_namespace values: {namespaces}"
    )
    for a in namespaces:
        for b in namespaces:
            if a is b:
                continue
            assert not a.startswith(f"{b}."), (
                f"{a!r} nests under {b!r} — servers must be siblings, not "
                "ancestor/descendant, in the logging hierarchy"
            )


def test_logger_namespace_is_under_the_package_namespace():
    """``configure_logging`` attaches handlers at LOGGER_ROOT; this is the
    nesting that lets every server's records still reach them."""
    for spec in ALL_SPECS:
        assert spec.logger_namespace.startswith(f"{LOGGER_NAMESPACE}."), (
            f"{spec.id}: logger_namespace {spec.logger_namespace!r} is not "
            f"under {LOGGER_NAMESPACE!r}"
        )


def test_default_ports_differ():
    """Two servers left on one port cannot both bind (see ServerSpec.default_port)."""
    ports = [spec.default_port for spec in ALL_SPECS]
    assert len(ports) == len(set(ports)), f"duplicate default_port values: {ports}"


def test_default_log_files_differ():
    """RotatingFileHandler is not multi-process safe (see ServerSpec.default_log_file)."""
    log_files = [spec.default_log_file for spec in ALL_SPECS]
    assert len(log_files) == len(set(log_files)), (
        f"duplicate default_log_file values: {log_files}"
    )


def _name_counts() -> dict[str, int]:
    """How many servers expose each tool name."""
    counts: dict[str, int] = {}
    for spec in ALL_SPECS:
        for name in spec.tools.all_tool_names:
            counts[name] = counts.get(name, 0) + 1
    return counts


def test_tool_names_are_globally_unique_except_the_known_duplicates():
    """A client connected to two servers sees one flat tool namespace.

    Any name on more than one server that is neither deliberately shared nor
    a grandfathered duplicate is new and unintended, and must fail here
    rather than surface as ambiguous tool dispatch in a multi-server client.
    """
    unexpected = {
        name
        for name, count in _name_counts().items()
        if count > 1 and name not in _EXPECTED_MULTI_SERVER_NAMES
    }
    assert not unexpected, (
        f"tool name(s) {sorted(unexpected)} appear on more than one server and "
        "are in neither SHARED_TOOL_NAMES nor KNOWN_DUPLICATE_TOOL_NAMES. If "
        "every server registers the same function, add it to the former; if "
        "each has its own implementation, rename, or add it to the latter "
        "with a reason."
    )


def test_neither_allow_list_goes_stale():
    """A name that no longer appears twice must not stay listed.

    Without this, removing a server or renaming a tool leaves an entry that
    silences a *future* collision on that same name.
    """
    counts = _name_counts()
    for name in _EXPECTED_MULTI_SERVER_NAMES:
        assert counts.get(name, 0) >= 2, (
            f"{name!r} is allow-listed as appearing on multiple servers but "
            f"now appears on {counts.get(name, 0)} — remove it from "
            "SHARED_TOOL_NAMES / KNOWN_DUPLICATE_TOOL_NAMES."
        )


def test_shared_tools_are_literally_the_same_function():
    """SHARED means one implementation, not two that agree today.

    This is what separates a shared tool from a duplicate name. If a server
    ever registers its own ``get_server_configuration_status``, a client
    connected to both would get different behaviour from the same tool name —
    so that belongs in KNOWN_DUPLICATE_TOOL_NAMES, and this fails until it is
    moved there.
    """
    for name in SHARED_TOOL_NAMES:
        implementations = {
            spec.id: fn
            for spec in ALL_SPECS
            for fn in spec.tools.all_tools
            if fn.__name__ == name
        }
        distinct = {id(fn) for fn in implementations.values()}
        assert len(distinct) == 1, (
            f"{name!r} is in SHARED_TOOL_NAMES but "
            f"{sorted(implementations)} register different function objects. "
            "Either import the one shared implementation, or move the name to "
            "KNOWN_DUPLICATE_TOOL_NAMES."
        )


def test_shared_tools_are_registered_by_every_server():
    """A 'shared' tool missing from a server is a gap, not a choice.

    The reason this tool is shared at all is that every server needs to be
    able to report its own configuration without a cluster; a server that
    skips it silently loses first-line support.
    """
    for name in SHARED_TOOL_NAMES:
        missing = [
            spec.id for spec in ALL_SPECS if name not in spec.tools.all_tool_names
        ]
        assert not missing, (
            f"{name!r} is in SHARED_TOOL_NAMES but {missing} do not register "
            "it. Add it to that server's ToolSet, or stop calling it shared."
        )


def test_every_tool_has_annotations():
    """Every registerable tool needs MCP annotations (readOnlyHint, etc.)."""
    for spec in ALL_SPECS:
        missing = spec.tools.all_tool_names - set(spec.annotations)
        assert not missing, f"{spec.id}: tools missing annotations: {sorted(missing)}"


def test_scope_hints_reference_real_tools():
    """A hint for a removed/renamed tool is dead configuration."""
    for spec in ALL_SPECS:
        unknown_hints = set(spec.scope_hints) - spec.tools.all_tool_names
        assert not unknown_hints, (
            f"{spec.id}: scope_hints reference unknown tool(s): {sorted(unknown_hints)}"
        )


def test_at_most_one_spec_owns_each_sdk_log_hook():
    """``couchbase.configure_logging`` (and any SDK's equivalent) is one-shot
    per process; only one server may be the one that calls it."""
    hooks_by_identity: dict[int, list[str]] = {}
    for spec in ALL_SPECS:
        if spec.sdk_log_hook is None:
            continue
        hooks_by_identity.setdefault(id(spec.sdk_log_hook), []).append(spec.id)
    for owners in hooks_by_identity.values():
        assert len(owners) == 1, f"sdk_log_hook shared by multiple specs: {owners}"
