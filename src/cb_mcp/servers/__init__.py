"""Concrete MCP servers built on the shared core.

Each module here declares one server as a :class:`~cb_mcp.core.spec.ServerSpec`
— its tools, scope labels, logging namespace and SDK hook — which
:func:`cb_mcp.core.app.build_app` turns into a runnable application.

Specs are imported by the host that runs them, never eagerly from this package,
so that a process only imports the SDK belonging to the server it is actually
running.
"""
