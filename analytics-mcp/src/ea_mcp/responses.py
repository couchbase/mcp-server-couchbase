"""Standard result envelopes for write/mutation MCP tools.

Copied verbatim from the parent ``cb_mcp.utils.responses`` module so the
envelope shape matches exactly for an eventual copy-paste into the real EA
tool set.
"""

from typing import Any


def tool_success(**fields: Any) -> dict[str, Any]:
    """Build a success envelope: ``{"success": True, **fields}``."""
    return {"success": True, **fields}


def tool_error(error: Exception | str, **fields: Any) -> dict[str, Any]:
    """Build a failure envelope: ``{"success": False, "error": str(error), **fields}``."""
    return {"success": False, "error": str(error), **fields}
