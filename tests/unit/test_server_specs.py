"""Cross-server invariants declared by ``core/spec.py`` but never checked.

``ServerSpec``'s docstrings document several rules that only matter once a
second server exists: ports and log files must differ, namespaces must nest
under the package root, tool names should be unique, and so on. Before the
Operational Insights server, there was nothing to check these against. This
file is that check, over every spec in ``tests/_all_specs.ALL_SPECS``.
"""

from _all_specs import ALL_SPECS

from cb_mcp.utils.constants import LOGGER_NAMESPACE

# Tool names deliberately shared between servers, and why. Per
# CONTRIBUTING.md's tool-naming section: the operational and Operational
# Insights servers each run as an independent process, so a name collision
# only matters to a client that registers both simultaneously. Renaming was
# considered and declined — these are the ported prototype's original names,
# and one process runs one server. This allow-list exists so that a *new*,
# unintended collision still fails the build; it does not silence this one.
KNOWN_DUPLICATE_TOOL_NAMES = frozenset(
    {
        "get_collections_in_scope",
        "get_schema_for_collection",
        "create_index",
    }
)


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


def test_tool_names_are_globally_unique_except_the_known_duplicates():
    """A client connected to two servers sees one flat tool namespace.

    Any collision not already named in KNOWN_DUPLICATE_TOOL_NAMES is new and
    unintended, and must fail here rather than surface as ambiguous tool
    dispatch in a multi-server client.
    """
    seen: dict[str, str] = {}
    unexpected_duplicates: set[str] = set()
    for spec in ALL_SPECS:
        for name in spec.tools.all_tool_names:
            if (
                name in seen
                and seen[name] != spec.id
                and name not in KNOWN_DUPLICATE_TOOL_NAMES
            ):
                unexpected_duplicates.add(name)
            seen[name] = spec.id
    assert not unexpected_duplicates, (
        f"tool name(s) {sorted(unexpected_duplicates)} collide across servers "
        "and are not in KNOWN_DUPLICATE_TOOL_NAMES — either rename, or add "
        "them there with a reason."
    )

    # The allow-list itself must not silently grow stale: every name in it
    # should still actually collide, and still actually exist.
    counts: dict[str, int] = {}
    for spec in ALL_SPECS:
        for name in spec.tools.all_tool_names:
            counts[name] = counts.get(name, 0) + 1
    for name in KNOWN_DUPLICATE_TOOL_NAMES:
        assert counts.get(name, 0) >= 2, (
            f"{name!r} is listed in KNOWN_DUPLICATE_TOOL_NAMES but no longer "
            "collides — remove it from the allow-list"
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
