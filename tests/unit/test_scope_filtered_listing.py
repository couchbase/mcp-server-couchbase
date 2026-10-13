"""Unit tests for authorization-scoped tool listing.

CBSE-23801. Adds no tools.

The test that matters most is the anti-drift one: everything ``tools/list``
offers must be permitted by the call path, and everything the call path permits
must be offered. In our own server these two reasoned separately, and that is
exactly how the disclosure gap arose, so it is asserted here rather than
assumed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from _all_specs import ALL_SPECS, SPEC_BY_ID
from fastmcp import Client

from cb_mcp.core.app import build_app
from cb_mcp.utils.scope_enforcement import required_scopes_for_tool
from cb_mcp.utils.scope_listing import ScopeFilteredListing, permitted_tool_names


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _token(*scopes: str) -> SimpleNamespace:
    return SimpleNamespace(scopes=list(scopes))


async def _list_with(middleware: ScopeFilteredListing, names: list[str], token):
    async def call_next(_context):
        return [_tool(n) for n in names]

    with patch("cb_mcp.utils.scope_listing.get_access_token", return_value=token):
        result = await middleware.on_list_tools(SimpleNamespace(), call_next)
    return [t.name for t in result]


def _middleware_for(spec) -> ScopeFilteredListing:
    return ScopeFilteredListing(
        write_tool_names=spec.tools.write_tool_names,
        scopes=spec.scopes,
        server_id=spec.id,
    )


ALL_SPEC_IDS = [spec.id for spec in ALL_SPECS]


@pytest.mark.parametrize("spec_id", ALL_SPEC_IDS)
class TestFilteringByScope:
    @pytest.mark.asyncio
    async def test_no_token_changes_nothing(self, spec_id: str) -> None:
        """stdio, or HTTP with OAuth not configured. A developer on a laptop
        must see exactly what they see today."""
        spec = SPEC_BY_ID[spec_id]
        names = sorted(spec.tools.all_tool_names)
        offered = await _list_with(_middleware_for(spec), names, None)
        assert offered == names

    @pytest.mark.asyncio
    async def test_a_reader_token_is_offered_no_write_tools(self, spec_id: str) -> None:
        spec = SPEC_BY_ID[spec_id]
        names = sorted(spec.tools.all_tool_names)
        offered = await _list_with(
            _middleware_for(spec), names, _token(spec.scopes.read)
        )
        assert set(offered) == set(spec.tools.read_only_tool_names)
        assert not set(offered) & set(spec.tools.write_tool_names)

    @pytest.mark.asyncio
    async def test_a_write_token_is_offered_no_read_tools(self, spec_id: str) -> None:
        """The two scopes are independent, not a hierarchy. A write-only token
        reaching read tools would be the same defect in the other direction."""
        spec = SPEC_BY_ID[spec_id]
        names = sorted(spec.tools.all_tool_names)
        offered = await _list_with(
            _middleware_for(spec), names, _token(spec.scopes.write)
        )
        assert set(offered) == set(spec.tools.write_tool_names)

    @pytest.mark.asyncio
    async def test_both_scopes_are_offered_everything(self, spec_id: str) -> None:
        spec = SPEC_BY_ID[spec_id]
        names = sorted(spec.tools.all_tool_names)
        offered = await _list_with(
            _middleware_for(spec), names, _token(spec.scopes.read, spec.scopes.write)
        )
        assert set(offered) == set(spec.tools.all_tool_names)

    @pytest.mark.asyncio
    async def test_a_token_with_no_scopes_is_offered_nothing(
        self, spec_id: str
    ) -> None:
        spec = SPEC_BY_ID[spec_id]
        names = sorted(spec.tools.all_tool_names)
        offered = await _list_with(_middleware_for(spec), names, _token())
        assert offered == []

    @pytest.mark.asyncio
    async def test_an_unrelated_scope_grants_nothing(self, spec_id: str) -> None:
        spec = SPEC_BY_ID[spec_id]
        names = sorted(spec.tools.all_tool_names)
        offered = await _list_with(
            _middleware_for(spec), names, _token("some-other-product:admin")
        )
        assert offered == []


@pytest.mark.parametrize("spec_id", ALL_SPEC_IDS)
class TestListingAndCallingCannotDrift:
    """The assertion this whole change exists to make true."""

    @pytest.mark.parametrize("which", ["read", "write", "both", "none"])
    def test_offered_equals_permitted(self, spec_id: str, which: str) -> None:
        spec = SPEC_BY_ID[spec_id]
        held = {
            "read": {spec.scopes.read},
            "write": {spec.scopes.write},
            "both": {spec.scopes.read, spec.scopes.write},
            "none": set(),
        }[which]
        names = sorted(spec.tools.all_tool_names)

        offered = set(
            permitted_tool_names(
                names,
                held_scopes=held,
                write_tool_names=spec.tools.write_tool_names,
                scopes=spec.scopes,
            )
        )
        # Recomputed independently, straight from the call path's own function,
        # rather than reusing the listing helper's answer.
        permitted = {
            name
            for name in names
            if not required_scopes_for_tool(
                name,
                write_tool_names=spec.tools.write_tool_names,
                scopes=spec.scopes,
            )
            - held
        }

        assert offered == permitted, (
            "tools/list and the call path disagree. Every tool advertised must "
            "be invocable, and every invocable tool must be advertised."
        )


@pytest.mark.parametrize("spec_id", ALL_SPEC_IDS)
class TestTheFilterIsNotTheClassification:
    """The read/write split is not duplicated here; it is read from the spec."""

    def test_the_split_comes_from_the_spec(self, spec_id: str) -> None:
        spec = SPEC_BY_ID[spec_id]
        assert set(spec.tools.read_only_tool_names) | set(
            spec.tools.write_tool_names
        ) == set(spec.tools.all_tool_names)
        assert not set(spec.tools.read_only_tool_names) & set(
            spec.tools.write_tool_names
        )


class TestRegistrationInBuildApp:
    """``build_app`` must actually install the filter.

    The tests above call the middleware directly, so removing the registration
    in ``build_app`` would leave all of them passing while ``tools/list`` went
    back to advertising every tool. These go through a real FastMCP client.
    """

    @staticmethod
    async def _listed(spec, *, enforce_scopes: bool, token) -> set[str]:
        mcp = build_app(
            spec,
            tools=list(spec.tools.all_tools),
            settings={"transport": "stdio"},
            provider_factory=MagicMock,
            read_only_mode=False,
            send_startup_ping=False,
            enforce_scopes=enforce_scopes,
        )
        with patch("cb_mcp.utils.scope_listing.get_access_token", return_value=token):
            async with Client(mcp) as client:
                return {tool.name for tool in await client.list_tools()}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spec_id", ALL_SPEC_IDS)
    async def test_a_reader_is_not_offered_write_tools(self, spec_id: str) -> None:
        spec = SPEC_BY_ID[spec_id]
        write = set(spec.tools.write_tool_names)
        listed = await self._listed(
            spec, enforce_scopes=True, token=_token(spec.scopes.read)
        )

        assert listed, "a reader should still be offered the read tools"
        assert not (listed & write), sorted(listed & write)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spec_id", ALL_SPEC_IDS)
    async def test_without_enforcement_nothing_is_filtered(self, spec_id: str) -> None:
        """The control: proves the assertion above depends on the registration."""
        spec = SPEC_BY_ID[spec_id]
        listed = await self._listed(
            spec, enforce_scopes=False, token=_token(spec.scopes.read)
        )

        assert listed >= set(spec.tools.write_tool_names)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spec_id", ALL_SPEC_IDS)
    async def test_both_scopes_are_offered_everything(self, spec_id: str) -> None:
        spec = SPEC_BY_ID[spec_id]
        listed = await self._listed(
            spec,
            enforce_scopes=True,
            token=_token(spec.scopes.read, spec.scopes.write),
        )

        assert listed >= set(spec.tools.write_tool_names)
        assert listed >= set(spec.tools.read_only_tool_names)
