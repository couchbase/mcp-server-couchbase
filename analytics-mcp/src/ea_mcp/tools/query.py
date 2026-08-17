"""Query execution tools for Enterprise Analytics.

run_query_sync can carry DDL/DML, so — unlike the metadata tools in this
package — it follows the parent server's write-tool convention: catch
Exception, log, and return a {"success": False, "error": ...} envelope
instead of raising.
"""

import logging
from typing import Any

from fastmcp import Context

from ..connection import get_cluster_connection
from ..responses import tool_error, tool_success

logger = logging.getLogger("ea-mcp-server.tools.query")


def run_query_sync(ctx: Context, statement: str) -> dict[str, Any]:
    """Run a SQL++ statement and buffer all result rows in memory.

    Can carry SELECT, DML, or DDL statements. Buffers the entire result set
    in client memory before returning.

    Returns {"success": True, "rows": [...], "row_count": N} on success, or
    {"success": False, "error": "..."} on failure.
    """
    cluster = get_cluster_connection(ctx)
    try:
        logger.debug("Running SQL++ statement synchronously")
        result = cluster.execute_query(statement)
        rows = result.get_all_rows()
        logger.info(f"Query returned {len(rows)} row(s)")
        return tool_success(rows=rows, row_count=len(rows))
    except Exception as e:
        logger.error(f"Error running query: {e}", exc_info=True)
        return tool_error(e, statement=statement)
