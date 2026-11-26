"""Unit tests for agent-declared tool set selection.

CBSE-23802, part 2.

The invariant under test is the one that makes this safe rather than dangerous:
selection can only narrow. Everything else here exists to make sure that stays
true under the cases where it would be tempting to widen.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from _all_specs import SPEC_BY_ID
from fastmcp import Client, FastMCP

from cb_mcp.tool_registration import prepare_tools_for_registration
from cb_mcp.tools import selection
from cb_mcp.tools.selection import (
    SELECTION_STATE_KEY,
    SELECTION_TOOL_NAME,
    AgentSelectedToolSets,
    make_select_tool_sets,
)

SPEC = SPEC_BY_ID["operational"]


class _SessionState:
    """Stands in for FastMCP's session-scoped state store.

    One shared dict keyed by ``(session_id, key)`` is what the real store does:
    a value set in one session is invisible to every other.
    """

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def bind(self, session_id: str | None) -> dict[str, object]:
        async def set_state(key, value, **_kw):
            self.data[(session_id, key)] = value

        async def get_state(key):
            return self.data.get((session_id, key))

        async def delete_state(key):
            self.data.pop((session_id, key), None)

        return {
            "set_state": set_state,
            "get_state": get_state,
            "delete_state": delete_state,
        }


@pytest.fixture
def state() -> _SessionState:
    return _SessionState()


def _ctx(state: _SessionState, session_id: str | None = "s1") -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        session=SimpleNamespace(send_tool_list_changed=AsyncMock()),
        **state.bind(session_id),
    )


def selected(state: _SessionState, session_id: str):
    """The set names a session selected, or None if it has not selected."""
    stored = state.data.get((session_id, SELECTION_STATE_KEY))
    return None if stored is None else frozenset(stored)


async def _offered(
    spec, names: list[str], session_id: str | None, state: _SessionState
) -> list[str]:
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
        return_value=SimpleNamespace(session_id=session_id, **state.bind(session_id)),
    ):
        result = await mw.on_list_tools(SimpleNamespace(), call_next)
    return [t.name for t in result]


class TestSelecting:
    @pytest.mark.asyncio
    async def test_selecting_a_set_records_it(self, state: _SessionState) -> None:
        tool = make_select_tool_sets(SPEC)
        result = await tool(_ctx(state, "s1"), ["kv"])
        assert result["success"] is True
        assert result["selected_tool_sets"] == ["kv"]
        assert selected(state, "s1") == frozenset({"kv"})

    @pytest.mark.asyncio
    async def test_the_client_is_told_the_list_changed(
        self, state: _SessionState
    ) -> None:
        """The standard mechanism. Without the notification the client keeps
        showing a list the server no longer serves."""
        ctx = _ctx(state, "s1")
        await make_select_tool_sets(SPEC)(ctx, ["kv"])
        ctx.session.send_tool_list_changed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_client_that_cannot_be_notified_still_succeeds(
        self, state: _SessionState
    ) -> None:
        """A client without listChanged support gets the narrowed list on its
        next fetch. That is a degraded experience, not a failed call."""
        ctx = _ctx(state, "s1")
        ctx.session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("no notifications here")
        )
        result = await make_select_tool_sets(SPEC)(ctx, ["kv"])
        assert result["success"] is True
        assert selected(state, "s1") == frozenset({"kv"})

    @pytest.mark.asyncio
    async def test_an_empty_list_clears_the_selection(
        self, state: _SessionState
    ) -> None:
        tool = make_select_tool_sets(SPEC)
        await tool(_ctx(state, "s1"), ["kv"])
        result = await tool(_ctx(state, "s1"), [])
        assert result["success"] is True
        assert selected(state, "s1") is None

    @pytest.mark.asyncio
    async def test_a_connection_without_a_session_is_told_so(
        self, state: _SessionState
    ) -> None:
        ctx = SimpleNamespace(
            session_id=None,
            session=SimpleNamespace(send_tool_list_changed=AsyncMock()),
        )
        result = await make_select_tool_sets(SPEC)(ctx, ["kv"])
        assert result["success"] is False
        assert "no session" in result["error"]
        assert not state.data
        ctx.session.send_tool_list_changed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unknown_set_is_refused_and_names_the_real_ones(
        self, state: _SessionState
    ) -> None:
        result = await make_select_tool_sets(SPEC)(
            _ctx(state, "s1"), ["kv", "nonsense"]
        )
        assert result["success"] is False
        assert "nonsense" in result["error"]
        assert result["available_tool_sets"] == sorted(SPEC.tool_sets)

    @pytest.mark.asyncio
    async def test_a_refused_call_changes_nothing(self, state: _SessionState) -> None:
        tool = make_select_tool_sets(SPEC)
        await tool(_ctx(state, "s1"), ["kv"])
        await tool(_ctx(state, "s1"), ["nonsense"])
        assert selected(state, "s1") == frozenset({"kv"})


class TestSelectionOnlyNarrows:
    """The safety invariant, asserted rather than asserted-in-prose."""

    @pytest.mark.asyncio
    async def test_no_selection_offers_everything(self, state: _SessionState) -> None:
        names = sorted(SPEC.tools.all_tool_names)
        assert await _offered(SPEC, names, "s1", state) == names

    @pytest.mark.asyncio
    async def test_a_selection_offers_only_that_set(self, state: _SessionState) -> None:
        # The selection tool is registered alongside the spec's tools rather
        # than inside a tool set, so the list reaching the middleware contains
        # it. Handing in only the spec's names would ask the middleware to
        # produce a tool nobody offered it, which it cannot and must not do.
        await make_select_tool_sets(SPEC)(_ctx(state, "s1"), ["kv"])
        names = [*sorted(SPEC.tools.all_tool_names), SELECTION_TOOL_NAME]
        offered = set(await _offered(SPEC, names, "s1", state))
        assert offered == set(SPEC.tool_sets["kv"]) | {SELECTION_TOOL_NAME}

    @pytest.mark.asyncio
    async def test_it_can_never_add_a_tool_the_caller_was_not_offered(
        self, state: _SessionState
    ) -> None:
        """The whole invariant in one assertion. The middleware filters what it
        was handed; whatever authorization already removed stays removed."""
        already_narrowed = sorted(SPEC.tool_sets["kv"])[:2]
        await make_select_tool_sets(SPEC)(_ctx(state, "s1"), ["kv", "query", "indexes"])
        offered = await _offered(SPEC, already_narrowed, "s1", state)
        assert set(offered) <= set(already_narrowed)

    @pytest.mark.asyncio
    async def test_the_way_back_is_never_narrowed_away(
        self, state: _SessionState
    ) -> None:
        """An agent that selected a set and then could not see this tool would
        have no way to widen again. A one-way door is a trap, not a control."""
        await make_select_tool_sets(SPEC)(_ctx(state, "s1"), ["query"])
        names = [*sorted(SPEC.tools.all_tool_names), SELECTION_TOOL_NAME]
        assert SELECTION_TOOL_NAME in await _offered(SPEC, names, "s1", state)

    @pytest.mark.asyncio
    async def test_selections_do_not_leak_between_sessions(
        self, state: _SessionState
    ) -> None:
        await make_select_tool_sets(SPEC)(_ctx(state, "s1"), ["kv"])
        names = sorted(SPEC.tools.all_tool_names)
        assert await _offered(SPEC, names, "s2", state) == names

    @pytest.mark.asyncio
    async def test_a_session_without_an_id_is_not_narrowed(
        self, state: _SessionState
    ) -> None:
        """stdio and anything else without a session id: nothing to key on, so
        nothing is narrowed. Narrowing on a guess would be the wrong default."""
        names = sorted(SPEC.tools.all_tool_names)
        assert await _offered(SPEC, names, None, state) == names


class TestTheToolDescribesItselfHonestly:
    def test_it_says_it_cannot_grant_anything(self) -> None:
        doc = " ".join((make_select_tool_sets(SPEC).__doc__ or "").split())
        assert "can only ever REDUCE" in doc

    def test_it_says_how_to_go_back(self) -> None:
        doc = " ".join((make_select_tool_sets(SPEC).__doc__ or "").split())
        assert "empty list" in doc


class TestOperatorControlsApplyToTheSelector:
    """``select_tool_sets`` goes through the same gating as every other tool."""

    @staticmethod
    def _prepare(**kwargs):
        return prepare_tools_for_registration(
            SPEC,
            read_only_mode=False,
            disabled_tools=kwargs.get("disabled_tools"),
            confirmation_required_tools=kwargs.get("confirmation_required_tools"),
            enable_tool_set_selection=True,
        )

    def test_it_is_registered_by_default_when_enabled(self) -> None:
        tools, _confirm, disabled = self._prepare()
        assert SELECTION_TOOL_NAME in {t.__name__ for t in tools}
        assert SELECTION_TOOL_NAME not in disabled

    def test_disabled_tools_can_remove_it(self) -> None:
        tools, _confirm, disabled = self._prepare(disabled_tools=SELECTION_TOOL_NAME)
        assert SELECTION_TOOL_NAME not in {t.__name__ for t in tools}
        assert SELECTION_TOOL_NAME in disabled

    def test_confirmation_can_be_required_for_it(self) -> None:
        _tools, confirm, _disabled = self._prepare(
            confirmation_required_tools=SELECTION_TOOL_NAME
        )
        assert SELECTION_TOOL_NAME in confirm


class TestSelectionIsHeldPerSessionByFastMCP:
    """Through a real FastMCP server, with no module-level selection store."""

    @staticmethod
    def _server():
        mcp = FastMCP("selection-test")
        mcp.add_middleware(AgentSelectedToolSets(spec=SPEC))

        def make(name: str):
            def tool() -> str:
                return name

            tool.__name__ = name
            return tool

        for name in sorted(SPEC.tools.all_tool_names):
            mcp.tool(make(name))
        mcp.tool(make_select_tool_sets(SPEC))
        return mcp

    @pytest.mark.asyncio
    async def test_a_selection_narrows_only_its_own_session(self) -> None:
        mcp = self._server()
        everything = {t.name for t in await _list(mcp)}

        async with Client(mcp) as first, Client(mcp) as second:
            await first.call_tool("select_tool_sets", {"tool_sets": ["kv"]})
            narrowed = {t.name for t in await first.list_tools()}
            untouched = {t.name for t in await second.list_tools()}

        assert narrowed == set(SPEC.tool_sets["kv"]) | {SELECTION_TOOL_NAME}
        assert untouched == everything

    @pytest.mark.asyncio
    async def test_a_new_session_starts_unnarrowed(self) -> None:
        mcp = self._server()
        async with Client(mcp) as first:
            await first.call_tool("select_tool_sets", {"tool_sets": ["kv"]})
        everything = {t.name for t in await _list(mcp)}
        assert len(everything) > len(SPEC.tool_sets["kv"]) + 1

    def test_the_module_keeps_no_selection_store_of_its_own(self) -> None:
        assert not hasattr(selection, "_SELECTIONS")


async def _list(mcp):
    async with Client(mcp) as client:
        return await client.list_tools()
