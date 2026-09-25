"""The Operational Insights MCP server.

Kept inert on purpose: importing any submodule of this package runs this
file first, so if it imported the spec (which imports the tools, which
import the server's constants) that would be a circular import.
``tests/unit/test_logger_names.py`` imports each server package first in a
clean subprocess to catch this.
"""
