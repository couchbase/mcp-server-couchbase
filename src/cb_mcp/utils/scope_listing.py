"""Authorization-scoped tool listing.

CBSE-23801.

``tools/list`` is currently authenticated but not authorized. Every caller is
offered the whole catalogue, and ``wrap_with_scope_check`` then refuses the ones
the token cannot use at call time. That has two costs. The agent spends context
on tools it cannot invoke and will periodically select one and fail. And a
principal authorized only to read learns the name, description and full argument
schema of every privileged operation the server exposes, which in a regulated
account is a disclosure finding.

This module closes that by filtering the listing through **the same predicate
the call path already uses**, ``required_scopes_for_tool``. That is the whole
design, and the part worth protecting in review: listing and calling must share
an implementation, not an intention. Where they reason separately they drift,
and the drift is silent — the catalogue advertises what the call path refuses.

With no token in context (stdio, or HTTP with OAuth not configured) the filter
is a no-op, exactly as the call-path wrapper is, so the same code path serves
both modes and a developer on a laptop sees no change.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware

from ..core.spec import ScopeSpec
from .constants import LOGGER_NAMESPACE
from .scope_enforcement import required_scopes_for_tool

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.utils.scope_listing")


def permitted_tool_names(
    tool_names: Sequence[str],
    *,
    held_scopes: set[str],
    write_tool_names: set[str] | frozenset[str],
    scopes: ScopeSpec,
) -> list[str]:
    """The subset of ``tool_names`` a token holding ``held_scopes`` may invoke.

    Pure, and deliberately separate from the middleware, so the anti-drift test
    can compare it against the call path without constructing a server.
    """
    return [
        name
        for name in tool_names
        if not required_scopes_for_tool(
            name, write_tool_names=write_tool_names, scopes=scopes
        )
        - held_scopes
    ]


class ScopeFilteredListing(Middleware):
    """Advertise a tool if and only if the caller's token could invoke it."""

    def __init__(
        self,
        *,
        write_tool_names: set[str] | frozenset[str],
        scopes: ScopeSpec,
        server_id: str,
    ) -> None:
        self._write_tool_names = frozenset(write_tool_names)
        self._scopes = scopes
        self._server_id = server_id

    async def on_list_tools(self, context: Any, call_next: Any) -> Sequence[Any]:
        tools = await call_next(context)

        token = get_access_token()
        if token is None:
            # No token: stdio, or OAuth not configured. Nothing to filter by,
            # and inventing a restriction here would break the laptop case.
            return tools

        held = set(token.scopes or [])
        allowed = set(
            permitted_tool_names(
                [tool.name for tool in tools],
                held_scopes=held,
                write_tool_names=self._write_tool_names,
                scopes=self._scopes,
            )
        )
        filtered = [tool for tool in tools if tool.name in allowed]

        if len(filtered) != len(tools):
            logger.info(
                "Scope-filtered tools/list for server '%s': %d of %d offered.",
                self._server_id,
                len(filtered),
                len(tools),
            )
        return filtered
