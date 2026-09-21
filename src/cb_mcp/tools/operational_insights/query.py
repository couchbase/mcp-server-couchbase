"""Query execution tools for Operational Insights.

``run_query_sync`` can carry DDL/DML, so — unlike the metadata tools in this
package — it follows the operational server's write-tool convention: catch
Exception, log, and return a ``{"success": False, "error": ...}`` envelope
instead of raising.

``explain_query`` follows the same envelope convention.
"""

import logging
import re
from typing import Any

from couchbase_operational_insights.options import QueryOptions
from fastmcp import Context
from fastmcp.server.dependencies import get_access_token

from ...servers.operational_insights.constants import (
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)
from ...utils.constants import SCOPE_WRITE
from ...utils.operational_insights.context import get_oi_cluster
from ...utils.responses import tool_error, tool_success

logger = logging.getLogger(f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.tools.query")


def _is_copy_to_statement(statement: str) -> bool:
    """True for a ``COPY ... TO`` statement.

    Covers both forms — export to external object storage and export to a
    KV collection — since both share the same leading keyword; there is no
    need to parse the rest of the grammar to tell them apart here.
    """
    normalized = statement.lstrip().upper()
    return re.match(r"^COPY\s", normalized) is not None


def run_query_sync(ctx: Context, statement: str) -> dict[str, Any]:
    """Run a SQL++ statement and buffer all result rows in memory.

    Can carry SELECT, DML, or DDL statements. Buffers the entire result set
    in client memory before returning.

    When the server is in read-only mode, or the caller's token lacks the
    write scope, the statement is executed with ``QueryOptions(readonly=True)``
    so the Operational Insights server itself rejects DML/DDL — there is no
    client-side SQL++ parser here (unlike the operational server's
    ``run_sql_plus_plus_query``), so enforcement is otherwise entirely
    server-side.

    One statement needs an extra, client-side check on top of that:
    ``COPY ... TO`` is classified as read-only by the server itself (it
    doesn't modify anything already stored), so ``readonly=True`` alone does
    not block it — but it writes its result to external object storage or a
    KV collection, which this server's read-only guarantee still treats as a
    write. Blocked here via a regex on the leading keyword rather than a
    full grammar parser, since read-only mode is the only thing that cares
    about the distinction.

    Returns {"success": True, "rows": [...], "row_count": N} on success, or
    {"success": False, "error": "..."} on failure.
    """
    cluster = get_oi_cluster(ctx)

    app_context = ctx.request_context.lifespan_context
    read_only_mode = app_context.read_only_mode

    # Mirrors run_sql_plus_plus_query's scope-gap closure: a token holding
    # only SCOPE_READ must not be able to mutate through this tool, since the
    # per-tool scope wrapper classifies it as a read tool (see
    # tools/operational_insights/__init__.py TOOL_SET). When no token is
    # present (stdio / OAuth disabled), lacks_write_scope is False, so the
    # historical read_only_mode-only behavior is preserved.
    token = get_access_token()
    lacks_write_scope = token is not None and SCOPE_WRITE not in (token.scopes or [])
    enforce_readonly = read_only_mode or lacks_write_scope

    if enforce_readonly and _is_copy_to_statement(statement):
        logger.debug("Blocking COPY ... TO statement under read-only mode")
        return tool_error(
            "COPY ... TO is blocked under read-only mode: the server itself "
            "classifies it as read-only, but it writes its result to "
            "external storage or a KV collection.",
            statement=statement,
        )

    try:
        logger.debug(
            f"Running SQL++ statement synchronously (readonly={enforce_readonly})"
        )
        result = (
            cluster.execute_query(statement, QueryOptions(readonly=True))
            if enforce_readonly
            else cluster.execute_query(statement)
        )
        rows = result.get_all_rows()
        logger.info(f"Query returned {len(rows)} row(s)")
        return tool_success(rows=rows, row_count=len(rows))
    except Exception as e:
        logger.error(f"Error running query: {e}", exc_info=True)
        return tool_error(e, statement=statement)


def explain_query(ctx: Context, statement: str) -> dict[str, Any]:
    """Generate the query plan for a SQL++ statement using EXPLAIN, without executing it.

    Whether the plan is cost-based or rule-based depends on whether the
    optimizer has collected samples for the target collection(s) — this tool
    just returns whatever plan the optimizer produces.

    Pass the statement without an EXPLAIN keyword; it is added automatically.

    Deliberately does not pass QueryOptions(readonly=True): EXPLAIN does not
    execute the statement it plans, and a readonly session may refuse to
    even plan a DDL/DML statement.

    Returns {"success": True, "plan": [...]} on success, or
    {"success": False, "error": "..."} on failure.
    """
    cluster = get_oi_cluster(ctx)
    try:
        logger.debug("Running EXPLAIN for SQL++ statement")
        result = cluster.execute_query(f"EXPLAIN {statement}")
        rows = result.get_all_rows()
        logger.info(f"EXPLAIN returned {len(rows)} row(s)")
        return tool_success(plan=rows)
    except Exception as e:
        logger.error(f"Error running EXPLAIN: {e}", exc_info=True)
        return tool_error(e, statement=statement)
