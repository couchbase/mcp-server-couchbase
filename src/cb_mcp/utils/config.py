import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastmcp import Context

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.config")


def get_settings(ctx: Context) -> Mapping[str, Any]:
    """Return the settings mapping attached to the lifespan context.

    Settings live on ``AppContext.settings``, populated once by the CLI
    entrypoint when it builds the lifespan closure. Reading from ``ctx``
    keeps the values per-server-instance instead of a module global and
    works from FastMCP's threadpool workers.
    """
    return ctx.request_context.lifespan_context.settings


def _parse_file(file_path: Path, valid_tool_names: set[str]) -> set[str]:
    """Parse tool names from a file (one tool per line)."""
    tools: set[str] = set()
    invalid: set[str] = set()
    try:
        with open(file_path) as f:
            for raw_line in f:
                name = raw_line.strip()
                if not name or name.startswith("#"):
                    continue
                if name in valid_tool_names:
                    tools.add(name)
                else:
                    invalid.add(name)
        if invalid:
            logger.warning(
                f"Ignored invalid tool name(s) from file {file_path}: {sorted(invalid)}"
            )
        logger.debug(f"Loaded {len(tools)} tool name(s) from file: {file_path}")
    except OSError as e:
        logger.error(f"Failed to read tool names file {file_path.resolve()}: {e}")
    return tools


def _parse_comma_separated(value: str, valid_tool_names: set[str]) -> set[str]:
    """Parse comma-separated tool names."""
    tools: set[str] = set()
    invalid: set[str] = set()
    for part in value.split(","):
        name = part.strip()
        if name:
            if name in valid_tool_names:
                tools.add(name)
            else:
                invalid.add(name)
    if invalid:
        logger.warning(f"Ignored invalid tool name(s): {sorted(invalid)}")
    logger.debug(f"Parsed tool names from comma-separated string: {tools}")
    return tools


def parse_tool_names(
    tool_names_input: str | None,
    valid_tool_names: set[str],
) -> set[str]:
    """
    Parse tool names from CLI argument or environment variable.

    Supported formats:
    1. Comma-separated string: "tool_1,tool_2"
    2. File path containing one tool name per line: "disabled_tools.txt"

    Args:
        tool_names_input: Comma-separated tools or file path
        valid_tool_names: Set of valid tool names to validate against

    Returns:
        Set of valid tool names
    """
    if not tool_names_input:
        return set()

    value = tool_names_input.strip()

    # Check if it's a file path
    potential_path = Path(value)
    if potential_path.exists() and potential_path.is_file():
        return _parse_file(potential_path, valid_tool_names)

    # Otherwise, treat as comma-separated
    return _parse_comma_separated(value, valid_tool_names)
