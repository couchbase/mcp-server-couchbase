"""MCP tools, one subpackage per server.

Tools are grouped by the server that exposes them — see
:mod:`cb_mcp.tools.operational` — rather than being flattened here, because
each server's tools speak a different backing SDK and are registered as a
distinct :class:`~cb_mcp.core.spec.ToolSet`.

Deliberately empty of re-exports: a name like ``TOOL_SET`` is meaningless
without saying whose, so import from the server's subpackage explicitly.
"""
