"""Standard result envelopes for write/mutation MCP tools.

Write tools return a structured dict rather than a bare bool so the calling
LLM can see *why* an operation failed, not just that it did. The base shape is
``{"success": bool}`` plus ``"error"`` on failure; each tool adds
operation-specific context (``keyspace``, ``index_name``, ...) as keyword
arguments. KV write tools can adopt these helpers later for a uniform contract.
"""

from typing import Any


def tool_success(**fields: Any) -> dict[str, Any]:
    """Build a success envelope: ``{"success": True, **fields}``."""
    return {"success": True, **fields}


def tool_error(error: Exception | str, **fields: Any) -> dict[str, Any]:
    """Build a failure envelope: ``{"success": False, "error": str(error), **fields}``."""
    return {"success": False, "error": str(error), **fields}
