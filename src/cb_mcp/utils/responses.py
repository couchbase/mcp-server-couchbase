"""Standard result envelopes for MCP tools that report failure as data.

These tools return a structured dict rather than a bare bool so the calling
LLM can see *why* an operation failed, not just that it did. The base shape is
``{"success": bool}`` plus ``"error"`` on failure; each tool adds
operation-specific context (``keyspace``, ``index_name``, ...) as keyword
arguments. KV write tools can adopt these helpers later for a uniform contract.

Used by every write tool, and by read tools that catch their own failures rather
than letting them propagate (``discover_tool_input_values``). Read tools whose
only failure mode is an unreachable cluster still return bare data and raise.
"""

from typing import Any


def tool_success(**fields: Any) -> dict[str, Any]:
    """Build a success envelope: ``{"success": True, **fields}``."""
    return {"success": True, **fields}


def tool_error(error: Exception | str, **fields: Any) -> dict[str, Any]:
    """Build a failure envelope: ``{"success": False, "error": str(error), **fields}``."""
    return {"success": False, "error": str(error), **fields}
