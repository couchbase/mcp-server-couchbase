"""Unit tests for named tool sets.

CBSE-23802, part 1. Adds no tools.

Two things are being protected. That selecting a set narrows to exactly the
tools it names and nothing else, and that a set can never name a tool that does
not exist — which is checked across every server by reading the spec, with no
server started and no SDK imported, the property ``ServerSpec`` exists to give.
"""

from __future__ import annotations

import pytest
from _all_specs import ALL_SPECS, SPEC_BY_ID

from cb_mcp.tool_registration import prepare_tools_for_registration

ALL_SPEC_IDS = [spec.id for spec in ALL_SPECS]


def _register(spec, **kwargs):
    defaults = {
        "read_only_mode": False,
        "disabled_tools": None,
        "confirmation_required_tools": None,
    }
    defaults.update(kwargs)
    tools, _confirm, _disabled = prepare_tools_for_registration(spec, **defaults)
    return {tool.__name__ for tool in tools}


@pytest.mark.parametrize("spec_id", ALL_SPEC_IDS)
class TestEverySetNamesRealTools:
    """The cross-server invariant. Catches a set that names a tool another
    server owns, or one renamed out from under it, by reading the spec."""

    def test_every_name_in_every_set_exists(self, spec_id: str) -> None:
        spec = SPEC_BY_ID[spec_id]
        known = spec.tools.all_tool_names
        for set_name, members in spec.tool_sets.items():
            unknown = set(members) - known
            assert not unknown, (
                f"tool set '{set_name}' on server '{spec.id}' names "
                f"{sorted(unknown)}, which this server does not expose"
            )

    def test_no_set_is_empty(self, spec_id: str) -> None:
        """An empty set resolves to no tools and looks like a typo at the
        command line. Better to not declare it."""
        spec = SPEC_BY_ID[spec_id]
        for set_name, members in spec.tool_sets.items():
            assert members, f"tool set '{set_name}' on '{spec.id}' is empty"

    def test_the_sets_cover_every_tool(self, spec_id: str) -> None:
        """A tool in no set is unreachable once an operator starts using
        --tool-sets, which is a silent way to lose a capability."""
        spec = SPEC_BY_ID[spec_id]
        if not spec.tool_sets:
            # A server that declares no sets is unaffected by --tool-sets, so
            # there is nothing to cover. Asserted as vacuously true rather than
            # skipped: a skip in a suite that must be clean is a report nobody
            # reads, and this is a real pass, not an untested case.
            assert spec.tool_sets == {}
            return
        covered: set[str] = set()
        for members in spec.tool_sets.values():
            covered |= set(members)
        missing = spec.tools.all_tool_names - covered
        assert not missing, (
            f"server '{spec.id}' exposes {sorted(missing)}, which belong to no "
            "declared tool set and would be unreachable under --tool-sets"
        )


class TestSelectionNarrows:
    """Against the operational server, which is the one that declares sets."""

    @property
    def spec(self):
        return SPEC_BY_ID["operational"]

    def test_no_selection_loads_everything(self) -> None:
        """The default must be byte-for-byte today's behaviour."""
        assert _register(self.spec) == set(self.spec.tools.all_tool_names)
        assert _register(self.spec, tool_sets=None) == set(
            self.spec.tools.all_tool_names
        )
        assert _register(self.spec, tool_sets="") == set(self.spec.tools.all_tool_names)

    def test_one_set_loads_exactly_that_set(self) -> None:
        loaded = _register(self.spec, tool_sets="kv")
        assert loaded == set(self.spec.tool_sets["kv"])

    def test_two_sets_load_the_union(self) -> None:
        loaded = _register(self.spec, tool_sets="kv,query")
        expected = set(self.spec.tool_sets["kv"]) | set(self.spec.tool_sets["query"])
        assert loaded == expected

    def test_whitespace_around_names_is_tolerated(self) -> None:
        assert _register(self.spec, tool_sets=" kv , query ") == _register(
            self.spec, tool_sets="kv,query"
        )

    def test_a_repeated_set_is_not_a_problem(self) -> None:
        assert _register(self.spec, tool_sets="kv,kv") == _register(
            self.spec, tool_sets="kv"
        )

    def test_an_unknown_set_fails_at_startup_and_names_the_real_ones(self) -> None:
        """Loading nothing and leaving the operator to work out why is the
        failure mode this replaces, not one it should reproduce."""
        with pytest.raises(ValueError) as excinfo:
            _register(self.spec, tool_sets="kv,nonsense")
        message = str(excinfo.value)
        assert "nonsense" in message
        assert "kv" in message

    def test_read_only_mode_still_applies_within_a_set(self) -> None:
        """Narrowing is not a way around read-only mode."""
        loaded = _register(self.spec, tool_sets="kv", read_only_mode=True)
        assert loaded == set(self.spec.tool_sets["kv"]) & set(
            self.spec.tools.read_only_tool_names
        )
        assert not loaded & set(self.spec.tools.write_tool_names)

    def test_disabled_tools_still_applies_after_the_set_is_resolved(self) -> None:
        """The deny list composes with the allow list rather than replacing it."""
        loaded = _register(
            self.spec, tool_sets="kv", disabled_tools="delete_document_by_id"
        )
        assert "delete_document_by_id" not in loaded
        assert "get_document_by_id" in loaded

    def test_selection_can_only_narrow(self) -> None:
        """Across every declared set, the result is always a subset."""
        everything = set(self.spec.tools.all_tool_names)
        for name in self.spec.tool_sets:
            assert _register(self.spec, tool_sets=name) <= everything
