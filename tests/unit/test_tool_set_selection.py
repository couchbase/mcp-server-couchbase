"""Unit tests for agent-declared tool set selection.

CBSE-23802, part 2. Stacked on part 1.

The invariant under test is the one that makes this safe rather than dangerous:
selection can only narrow. Everything else here exists to make sure that stays
true under the cases where it would be tempting to widen.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from _all_specs import SPEC_BY_ID

from cb_mcp.tools.selection import (
    SELECTION_TOOL_NAME,
    AgentSelectedToolSets,
    clear_selection,
    current_selection,
    make_select_tool_sets,
)

SPEC = SPEC_BY_ID["operational"]


@pytest.fixture(autouse=True)
def _isolate_sessions():
    """Selection is process-global by design; tests must not leak into each other."""
    yield
    for sid in ("s1", "s2", "s3"):
        clear_selection(sid)


def _ctx(session_id: str = "s1") -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        session=SimpleNamespace(send_tool_list_changed=AsyncMock()),
    )


async def _offered(spec, names: list[str], session_id: str | None) -> list[str]:
    mw = AgentSelectedToolSets(spec=spec)

    async def call_next(_context):
        return [SimpleNamespace(name=n) for n in names]

    # Patched where it is *used*, not where it is defined: the module does
    # ``from fastmcp.server.dependencies import get_context`` at import time, so
    # the name in this module is the only one the middleware will look at.
    # Patching the origin instead left the real get_context in place, which
    # raises outside a request context, which the middleware treats as "no
    # context, offer everything" -- and the narrowing assertions below then
    # passed vacuously.
    with patch(
        "cb_mcp.tools.selection.get_context",
        return_value=SimpleNamespace(session_id=session_id),
    ):
        result = await mw.on_list_tools(SimpleNamespace(), call_next)
    return [t.name for t in result]


class TestSelecting:
    @pytest.mark.asyncio
    async def test_selecting_a_set_records_it(self) -> None:
        tool = make_select_tool_sets(SPEC)
        result = await tool(_ctx("s1"), ["kv"])
        assert result["success"] is True
        assert result["selected_tool_sets"] == ["kv"]
        assert current_selection("s1") == frozenset({"kv"})

    @pytest.mark.asyncio
    async def test_the_client_is_told_the_list_changed(self) -> None:
        """The standard mechanism. Without the notification the client keeps
        showing a list the server no longer serves."""
        ctx = _ctx("s1")
        await make_select_tool_sets(SPEC)(ctx, ["kv"])
        ctx.session.send_tool_list_changed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_client_that_cannot_be_notified_still_succeeds(self) -> None:
        """A client without listChanged support gets the narrowed list on its
        next fetch. That is a degraded experience, not a failed call."""
        ctx = _ctx("s1")
        ctx.session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("no notifications here")
        )
        result = await make_select_tool_sets(SPEC)(ctx, ["kv"])
        assert result["success"] is True
        assert current_selection("s1") == frozenset({"kv"})

    @pytest.mark.asyncio
    async def test_an_empty_list_clears_the_selection(self) -> None:
        tool = make_select_tool_sets(SPEC)
        await tool(_ctx("s1"), ["kv"])
        result = await tool(_ctx("s1"), [])
        assert result["success"] is True
        assert current_selection("s1") is None

    @pytest.mark.asyncio
    async def test_a_connection_without_a_session_is_told_so(self) -> None:
        ctx = SimpleNamespace(
            session_id=None,
            session=SimpleNamespace(send_tool_list_changed=AsyncMock()),
        )
        result = await make_select_tool_sets(SPEC)(ctx, ["kv"])
        assert result["success"] is False
        assert "no session" in result["error"]
        assert current_selection(None) is None
        ctx.session.send_tool_list_changed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unknown_set_is_refused_and_names_the_real_ones(self) -> None:
        result = await make_select_tool_sets(SPEC)(_ctx("s1"), ["kv", "nonsense"])
        assert result["success"] is False
        assert "nonsense" in result["error"]
        assert result["available_tool_sets"] == sorted(SPEC.tool_sets)

    @pytest.mark.asyncio
    async def test_a_refused_call_changes_nothing(self) -> None:
        tool = make_select_tool_sets(SPEC)
        await tool(_ctx("s1"), ["kv"])
        await tool(_ctx("s1"), ["nonsense"])
        assert current_selection("s1") == frozenset({"kv"})


class TestSelectionOnlyNarrows:
    """The safety invariant, asserted rather than asserted-in-prose."""

    @pytest.mark.asyncio
    async def test_no_selection_offers_everything(self) -> None:
        names = sorted(SPEC.tools.all_tool_names)
        assert await _offered(SPEC, names, "s1") == names

    @pytest.mark.asyncio
    async def test_a_selection_offers_only_that_set(self) -> None:
        # The selection tool is registered alongside the spec's tools rather
        # than inside a tool set, so the list reaching the middleware contains
        # it. Handing in only the spec's names would ask the middleware to
        # produce a tool nobody offered it, which it cannot and must not do.
        await make_select_tool_sets(SPEC)(_ctx("s1"), ["kv"])
        names = [*sorted(SPEC.tools.all_tool_names), SELECTION_TOOL_NAME]
        offered = set(await _offered(SPEC, names, "s1"))
        assert offered == set(SPEC.tool_sets["kv"]) | {SELECTION_TOOL_NAME}

    @pytest.mark.asyncio
    async def test_it_can_never_add_a_tool_the_caller_was_not_offered(self) -> None:
        """The whole invariant in one assertion. The middleware filters what it
        was handed; whatever authorization already removed stays removed."""
        already_narrowed = sorted(SPEC.tool_sets["kv"])[:2]
        await make_select_tool_sets(SPEC)(_ctx("s1"), ["kv", "query", "indexes"])
        offered = await _offered(SPEC, already_narrowed, "s1")
        assert set(offered) <= set(already_narrowed)

    @pytest.mark.asyncio
    async def test_the_way_back_is_never_narrowed_away(self) -> None:
        """An agent that selected a set and then could not see this tool would
        have no way to widen again. A one-way door is a trap, not a control."""
        await make_select_tool_sets(SPEC)(_ctx("s1"), ["query"])
        names = [*sorted(SPEC.tools.all_tool_names), SELECTION_TOOL_NAME]
        assert SELECTION_TOOL_NAME in await _offered(SPEC, names, "s1")

    @pytest.mark.asyncio
    async def test_selections_do_not_leak_between_sessions(self) -> None:
        await make_select_tool_sets(SPEC)(_ctx("s1"), ["kv"])
        names = sorted(SPEC.tools.all_tool_names)
        assert await _offered(SPEC, names, "s2") == names

    @pytest.mark.asyncio
    async def test_a_session_without_an_id_is_not_narrowed(self) -> None:
        """stdio and anything else without a session id: nothing to key on, so
        nothing is narrowed. Narrowing on a guess would be the wrong default."""
        names = sorted(SPEC.tools.all_tool_names)
        assert await _offered(SPEC, names, None) == names


class TestTheToolDescribesItselfHonestly:
    def test_it_says_it_cannot_grant_anything(self) -> None:
        doc = " ".join((make_select_tool_sets(SPEC).__doc__ or "").split())
        assert "can only ever REDUCE" in doc

    def test_it_says_how_to_go_back(self) -> None:
        doc = " ".join((make_select_tool_sets(SPEC).__doc__ or "").split())
        assert "empty list" in doc
