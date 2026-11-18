"""Agent-declared tool set selection, using the MCP standard mechanism.

CBSE-23802, part 2. Depends on part 1 (``ServerSpec.tool_sets``).

A single agent moves between jobs inside one session: provisioning an
environment, correcting data, then research over it. It should not carry the
tool surface for all three at once. ``--tool-sets`` answers that at launch, for
the whole process; this answers it per session, while the session is running.

The mechanism is ``tools/listChanged``, which is already in the MCP
specification and exists precisely for a server whose tool list changes during
a session. No protocol extension, no custom negotiation.

**The safety invariant, which is what makes this a usability feature rather
than a new attack surface:**

    Selection can only narrow. The effective list is
    ``authorized ∩ launch-selected ∩ agent-selected``, never a union.

An agent can put tools away. It can never hand itself a tool its credential
does not carry, or one the operator did not load. A compromised or confused
agent calling this can only reduce its own reach. Requesting a set it is not
authorized for yields the authorized subset of that set, not an error and not
an escalation, because an error would leak which tools exist.

Selection is per session and lives in memory. A reconnect is a new session and
starts unnarrowed, which is the safe direction: the floor is what the operator
and the credential allow, never less than that by accident.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from typing import Any

from fastmcp import Context
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import Middleware

from ..core.spec import ServerSpec
from ..utils.constants import LOGGER_NAMESPACE

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.tools.selection")

#: Session id -> the set names that session has selected. Absent means "no
#: selection made", which is different from "selected nothing": the first loads
#: everything the operator and credential allow, the second would load nothing.
_SELECTIONS: dict[str, frozenset[str]] = {}
_LOCK = threading.Lock()

#: The meta-tool is never narrowed away. An agent that selected a set and then
#: could not see this tool would have no way back, and a one-way door is a
#: usability trap rather than a safety property.
SELECTION_TOOL_NAME = "select_tool_sets"


def current_selection(session_id: str | None) -> frozenset[str] | None:
    """The set names this session selected, or None if it has not selected."""
    if session_id is None:
        return None
    with _LOCK:
        return _SELECTIONS.get(session_id)


def clear_selection(session_id: str | None) -> None:
    """Forget a session's selection. Called when a session ends."""
    if session_id is None:
        return
    with _LOCK:
        _SELECTIONS.pop(session_id, None)


def _record(session_id: str, names: frozenset[str]) -> None:
    with _LOCK:
        _SELECTIONS[session_id] = names


def make_select_tool_sets(spec: ServerSpec):
    """Build the ``select_tool_sets`` tool bound to one server's declared sets.

    A factory rather than a plain function because the tool has to validate
    against *this* server's sets, and a spec reached through a module global
    would be the cross-server coupling ``ServerSpec`` exists to prevent.
    """

    async def select_tool_sets(ctx: Context, tool_sets: list[str]) -> dict[str, Any]:
        """Narrow your own tool list to the named job-shaped sets.

        Call this once you know what job you are doing, to put away the tools
        that job does not need. Pass an empty list to go back to everything you
        are permitted.

        This can only ever REDUCE what you are offered. It cannot grant you a
        tool you do not already have, so there is no reason to call it hoping
        to gain access to something.

        Returns the set names now in effect and the tools they resolve to.
        Your client will refetch the tool list automatically.
        """
        session_id = getattr(ctx, "session_id", None)
        available = sorted(spec.tool_sets)

        if session_id is None:
            # Stateless HTTP (which --workers > 1 implies) keeps no session, so
            # there is nothing to remember a selection against. Saying so beats
            # reporting success for a narrowing the next call would not see.
            return {
                "success": False,
                "error": (
                    "This connection has no session, so a selection cannot be "
                    "remembered. Tool-set selection needs a stateful session "
                    "(not stateless HTTP, and not --workers greater than 1)."
                ),
                "available_tool_sets": available,
            }

        requested = [name.strip() for name in tool_sets if name and name.strip()]
        unknown = [name for name in requested if name not in spec.tool_sets]
        if unknown:
            # Named explicitly: these are the operator's own declared set
            # names, not a secret, and a vague error would send the agent
            # guessing.
            return {
                "success": False,
                "error": (
                    f"Unknown tool set(s) {sorted(unknown)}. "
                    f"Available: {', '.join(available) or '(none declared)'}."
                ),
                "available_tool_sets": available,
            }

        if not requested:
            clear_selection(session_id)
            resolved: list[str] = sorted(spec.tools.all_tool_names)
            selected_names: list[str] = []
        else:
            _record(session_id, frozenset(requested))
            members: set[str] = set()
            for name in requested:
                members |= set(spec.tool_sets[name])
            resolved = sorted(members)
            selected_names = sorted(requested)

        logger.info(
            "Session %s selected tool sets %s on server '%s'.",
            session_id,
            selected_names or "(all)",
            spec.id,
        )

        # The standard mechanism. Best effort: a client that does not support
        # the notification still gets the narrowed list on its next fetch, so a
        # failure here must not fail the call.
        # Broad by intent: the notification is advisory, and a client that
        # cannot take it must not see the call fail.
        try:
            await ctx.session.send_tool_list_changed()
        except Exception as e:
            logger.debug("Could not send tools/listChanged: %s", e)

        return {
            "success": True,
            "selected_tool_sets": selected_names,
            "available_tool_sets": available,
            # What the agent will actually see, before authorization and the
            # operator's own narrowing are applied on top. Those can only
            # subtract further.
            "tools_in_selection": resolved,
        }

    return select_tool_sets


class AgentSelectedToolSets(Middleware):
    """Apply a session's own selection to its tool list."""

    def __init__(self, *, spec: ServerSpec) -> None:
        self._spec = spec

    async def on_list_tools(self, context: Any, call_next: Any) -> Sequence[Any]:
        tools = await call_next(context)

        # Broad by intent: no request context means there is nothing to key a
        # selection on, and narrowing on a guess would be the wrong default.
        try:
            session_id = get_context().session_id
        except Exception:
            return tools

        selected = current_selection(session_id)
        if selected is None:
            return tools

        allowed: set[str] = set()
        for name in selected:
            allowed |= set(self._spec.tool_sets.get(name, ()))
        # Never narrow away the way back.
        allowed.add(SELECTION_TOOL_NAME)

        return [tool for tool in tools if tool.name in allowed]
