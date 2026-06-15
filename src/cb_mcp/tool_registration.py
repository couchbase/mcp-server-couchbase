"""
Tool registration orchestration shared across MCP implementations.
"""

import logging
from collections.abc import Callable

from .tools import KV_WRITE_TOOLS, get_tools
from .utils.config import parse_tool_names
from .utils.constants import MCP_SERVER_NAME
from .utils.elicitation import wrap_with_confirmation
from .utils.scope_enforcement import (
    TOOL_SCOPE_HINTS,
    required_scopes_for_tool,
    wrap_with_scope_check,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tool_registration")


def prepare_tools_for_registration(
    read_only_mode: bool,
    disabled_tools: str | None,
    confirmation_required_tools: str | None,
    enforce_scopes: bool = False,
) -> tuple[list[Callable], set[str], set[str]]:
    """Prepare final tool list and confirmation configuration for registration.

    Loads the shared cb_mcp tools, parses the disabled and confirmation lists,
    filters disabled tools out, and wraps tools with elicitation and (when
    OAuth is active) per-tool scope enforcement.

    Wrap order is ``scope_check ⟶ confirmation ⟶ tool``: the scope check
    runs first so unauthorized callers never trigger an elicitation prompt.
    Scope checks are no-ops at runtime when no access token is present
    (stdio / unauthenticated), so ``enforce_scopes`` only affects whether
    the wrapper is installed — not whether it does work per call.
    """
    # When read_only_mode is True, KV write tools are not loaded.
    tools = get_tools(read_only_mode=read_only_mode)

    loaded_tool_names = {tool.__name__ for tool in tools}
    disabled_tool_names = parse_tool_names(disabled_tools, loaded_tool_names)

    if disabled_tool_names:
        logger.info(
            f"Disabled {len(disabled_tool_names)} tool(s): {sorted(disabled_tool_names)}"
        )

    configured_confirmation_tool_names = parse_tool_names(
        confirmation_required_tools, loaded_tool_names
    )

    if configured_confirmation_tool_names:
        logger.info(
            f"Confirmation required for {len(configured_confirmation_tool_names)} tool(s): "
            f"{sorted(configured_confirmation_tool_names)}"
        )

    enabled_tools = [tool for tool in tools if tool.__name__ not in disabled_tool_names]

    # Apply confirmation only to tools that are actually active.
    active_tool_names = {tool.__name__ for tool in enabled_tools}
    active_confirmation_tool_names = (
        configured_confirmation_tool_names & active_tool_names
    )

    skipped_confirmation_tool_names = (
        configured_confirmation_tool_names - active_tool_names
    )
    if skipped_confirmation_tool_names:
        logger.info(
            "Skipped confirmation for unavailable tool(s): "
            f"{sorted(skipped_confirmation_tool_names)}"
        )

    write_tool_names = {fn.__name__ for fn in KV_WRITE_TOOLS}

    final_tools: list[Callable] = []
    for tool in enabled_tools:
        wrapped = tool
        if tool.__name__ in active_confirmation_tool_names:
            wrapped = wrap_with_confirmation(wrapped)
        if enforce_scopes:
            required_scopes = required_scopes_for_tool(
                tool.__name__, write_tool_names=write_tool_names
            )
            wrapped = wrap_with_scope_check(
                wrapped,
                required_scopes,
                hint=TOOL_SCOPE_HINTS.get(tool.__name__),
            )
        final_tools.append(wrapped)

    if enforce_scopes:
        logger.info(
            "Per-tool OAuth scope enforcement enabled for %d tool(s).",
            len(enabled_tools),
        )

    return final_tools, configured_confirmation_tool_names, disabled_tool_names
