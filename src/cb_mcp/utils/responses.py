"""Result helpers shared by MCP tools.

Successful write tools, and read tools with a success payload, return
``tool_success(...)``: ``{"success": True, **fields}``. Failure is signalled by
raising; see ``wrap_with_error_handling`` (``utils/error_handling.py``), which
turns the exception into the error the client sees.
"""

from typing import Any


def tool_success(**fields: Any) -> dict[str, Any]:
    """Build a success envelope: ``{"success": True, **fields}``."""
    return {"success": True, **fields}
