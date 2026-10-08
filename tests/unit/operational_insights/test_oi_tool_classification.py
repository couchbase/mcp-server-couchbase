"""Which Operational Insights tools are registered under read-only mode.

Nothing covered this split before, which is how a usability gap went
unnoticed: ``oi_cancel_async_query`` was write-classified, so under the default
``--read-only-mode`` a caller could start a long query with
``oi_run_query_async`` but had no registered tool to stop it — while
``oi_discard_async_query_results`` answered an in-flight handle by recommending
exactly that missing tool.

These tests pin the resulting invariant rather than just the current list: a
tool that *ends* a query the caller started must be reachable in every mode
that can start one.
"""

from cb_mcp.tools.operational_insights import TOOL_SET

#: The async lifecycle: start, poll, and the two ways to finish.
_LIFECYCLE = (
    "oi_run_query_async",
    "oi_get_async_query_results",
    "oi_discard_async_query_results",
    "oi_cancel_async_query",
)


def _registered(*, read_only_mode: bool) -> set[str]:
    return {fn.__name__ for fn in TOOL_SET.tools_for(read_only_mode=read_only_mode)}


class TestReadOnlyRegistration:
    def test_whole_async_lifecycle_is_reachable_in_read_only_mode(self) -> None:
        """The invariant: if you can start a query, you can also stop it.

        Registering ``oi_run_query_async`` without ``oi_cancel_async_query`` strands
        in-flight queries until the server times them out.
        """
        registered = _registered(read_only_mode=True)
        assert set(_LIFECYCLE) <= registered

    def test_cancel_is_registered_in_both_modes(self) -> None:
        assert "oi_cancel_async_query" in _registered(read_only_mode=True)
        assert "oi_cancel_async_query" in _registered(read_only_mode=False)

    def test_cancel_and_discard_share_a_classification(self) -> None:
        """Both end a caller's own query and neither touches stored data.

        Splitting them put the destructive-but-harmless pair on opposite sides
        of a boundary that exists to prevent *mutation*.
        """
        assert ("oi_cancel_async_query" in TOOL_SET.read_only_tool_names) == (
            "oi_discard_async_query_results" in TOOL_SET.read_only_tool_names
        )

    def test_create_index_remains_write_gated(self) -> None:
        """DDL is a genuine mutation, so it stays out of read-only mode."""
        assert "oi_create_index" in TOOL_SET.write_tool_names
        assert "oi_create_index" not in _registered(read_only_mode=True)

    def test_read_only_mode_registers_strictly_fewer_tools(self) -> None:
        assert _registered(read_only_mode=True) < _registered(read_only_mode=False)

    def test_every_tool_is_classified_exactly_once(self) -> None:
        """A tool in both tuples would be registered twice by build_app."""
        assert not (TOOL_SET.read_only_tool_names & TOOL_SET.write_tool_names)
