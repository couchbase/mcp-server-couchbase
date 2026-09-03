"""
Tool registration orchestration shared across MCP implementations.
"""

import logging
from collections.abc import Callable
from typing import Any

from fastmcp.tools import FunctionTool, ToolResult

from .tools import COLLECTION_WRITE_TOOLS, INDEX_WRITE_TOOLS, KV_WRITE_TOOLS, get_tools
from .utils import wrap_with_telemetry
from .utils.config import parse_tool_names
from .utils.constants import MCP_SERVER_NAME
from .utils.elicitation import wrap_with_confirmation
from .utils.scope_enforcement import (
    TOOL_SCOPE_HINTS,
    required_scopes_for_tool,
    wrap_with_scope_check,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.tool_registration")


class TextOnlyFunctionTool(FunctionTool):
    """A tool whose results never carry ``structuredContent``.

    Registering a tool with ``output_schema=None`` only drops the schema.
    FastMCP still attaches structured content to any result that serialises to
    a dict (see ``Tool.convert_result``), so a dict-returning tool would keep
    sending every result twice — once as JSON text, once as a structured
    object — with no schema for the client to validate it against. This strips
    that second copy, leaving the text block FastMCP already produced from the
    same value.

    Used by entrypoints that expose a "disable structured output" option, for
    clients that mishandle or reject structured tool results.
    """

    def convert_result(self, raw_value: Any) -> ToolResult:
        result = super().convert_result(raw_value)
        if result.structured_content is None:
            return result
        # model_copy rather than rebuilding the ToolResult: its constructor
        # re-runs content conversion, and there is nothing to re-convert here.
        return result.model_copy(update={"structured_content": None})


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

    Wrap order is ``scope_check ⟶ confirmation ⟶ telemetry ⟶ tool``: the scope
    check runs first so unauthorized callers never trigger an elicitation
    prompt. Scope checks are no-ops at runtime when no access token is present
    (stdio / unauthenticated), so ``enforce_scopes`` only affects whether
    the wrapper is installed — not whether it does work per call. Telemetry
    is innermost so its recorded duration/success reflects only the tool's
    own execution, excluding confirmation/scope-check overhead. A call
    rejected by the scope check or declined at confirmation never reaches
    the tool, so it never emits a tool-call event.
    """
    # When read_only_mode is True, write tools (KV, collection management, and
    # index management) are not loaded.
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

    write_tool_names = {
        fn.__name__
        for fn in KV_WRITE_TOOLS + COLLECTION_WRITE_TOOLS + INDEX_WRITE_TOOLS
    }

    final_tools: list[Callable] = []
    for tool in enabled_tools:
        wrapped = wrap_with_telemetry(tool)
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
