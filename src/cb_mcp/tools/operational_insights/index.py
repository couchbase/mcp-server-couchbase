"""Index management tools for Operational Insights.

The ``couchbase-operational-insights`` SDK exposes no index manager at all.
Index DDL therefore has to go through SQL++ ``CREATE INDEX``.

Live-cluster findings not stated by the published grammar (carried over from
the prototype's evaluation, since they exist nowhere else):
  - type is *optional* on a plain field but *mandatory* on an array-indexed
    field;
  - an array index *must* be created with ``exclude_unknown_key=True`` — the
    server rejects an array index without it, and rejects
    ``INCLUDE UNKNOWN KEY`` on an array index too;
  - ``CAST (DEFAULT NULL ...)`` is B-Tree only and cannot be combined with an
    array index;
  - the declared type is not validated against the underlying data — e.g.
    indexing a string field as ``double`` silently indexes nothing. Use
    ``get_schema_for_collection`` to check actual field types first.
"""

import logging
from typing import Any

from fastmcp import Context

from ...servers.operational_insights.constants import (
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)
from ...utils.operational_insights.context import get_oi_cluster
from ...utils.operational_insights.sqlpp import (
    keyspace,
    quote_literal,
    safe_field_path,
    safe_ident,
)
from ...utils.responses import tool_error, tool_success

logger = logging.getLogger(f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.tools.index")


def _format_cast_default(formats: dict[str, str] | None) -> str:
    """Render the optional CAST (DEFAULT NULL ...) clause.

    Grammar:
        IndexCastDefault   ::= "CAST" "(" "DEFAULT" "NULL" DateTimeFormatSpec? ")"
        DateTimeFormatSpec ::= ("DATE" StringLiteral)? ("TIME" StringLiteral)?
                               ("DATETIME" StringLiteral)?

    The keyword order is fixed by the grammar, so the formats dict is emitted
    in DATE, TIME, DATETIME order regardless of insertion order.
    """
    parts = []
    for keyword in ("date", "time", "datetime"):
        fmt = (formats or {}).get(keyword)
        if fmt is not None:
            parts.append(f"{keyword.upper()} {quote_literal(fmt)}")
    spec = (" " + " ".join(parts)) if parts else ""
    return f" CAST (DEFAULT NULL{spec})"


def _format_element(field: dict[str, Any]) -> str:
    """Render one IndexElement: "UNNEST ..." if it has 'unnest', else "path: type"."""
    if "unnest" not in field:
        name = safe_field_path(field["name"])
        field_type = field.get("type")
        return f"{name}: {field_type}" if field_type else name

    unnest = field["unnest"]
    paths = [unnest] if isinstance(unnest, str) else unnest
    clause = " ".join(f"UNNEST {safe_field_path(p)}" for p in paths)

    select = field.get("select")
    if select is None:
        return f"{clause}: {field['type']}"
    return f"{clause} SELECT " + ", ".join(
        f"{safe_field_path(s['name'])}: {s['type']}" for s in select
    )


def create_index(
    ctx: Context,
    database_name: str,
    scope_name: str,
    collection_name: str,
    index_name: str,
    fields: list[dict[str, Any]],
    if_not_exists: bool = False,
    exclude_unknown_key: bool = False,
    cast_default_null: bool = False,
    cast_formats: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a secondary index on an Operational Insights collection via CREATE INDEX.

    fields is a list of index elements. A plain field is {"name": ..., "type": ...},
    where "type" is optional (bigint, int, double, string, date, time, datetime) --
    e.g. [{"name": "title", "type": "string"}], or several for a composite index.
    Nested fields use a dotted path, e.g. "ratings.Lyrics".

    To index inside an array, use "unnest" instead of "name":
      - array of primitives: {"unnest": "public_likes", "type": "string"}
      - array of objects:    {"unnest": "reviews",
                              "select": [{"name": "ratings.Lyrics", "type": "bigint"}]}
      - nested arrays:       {"unnest": ["a", "b"], "type": "string"}
    A type is mandatory on array-indexed fields, and array indexes must also be
    created with exclude_unknown_key=True -- the server rejects an array index
    without it ("Array indexes must specify EXCLUDE UNKNOWN KEY."), and rejects
    INCLUDE UNKNOWN KEY on arrays too.

    Set cast_default_null=True to add CAST (DEFAULT NULL), which casts each
    value to the indexed type before indexing and stores NULL when the cast
    fails. Include it when the index supports a Tabular Analytics View (TAV).
    cast_formats gives non-ISO-8601 date/time formats and implies the clause,
    e.g. cast_formats={"date": "MM/DD/YYYY"} on a field indexed as date emits
    CAST (DEFAULT NULL DATE "MM/DD/YYYY"). Accepted keys: date, time, datetime.
    CAST is B-Tree only and cannot be combined with an array index.

    Note: this server and the operational server both expose a tool named
    ``create_index``. They run as separate processes/servers, so this is only
    a concern for a client that registers both simultaneously — see
    CONTRIBUTING.md's tool-naming section.

    Returns {"success": True, "index_name": ..., "keyspace": ..., "statement": ...},
    or {"success": False, "error": ...} on failure.
    """
    ks = keyspace(database_name, scope_name, collection_name)

    try:
        field_clause = ", ".join(_format_element(field) for field in fields)

        # A CAST clause is implied by passing formats, so callers need not set
        # both flags for the common "index a custom date format" case.
        cast_clause = (
            _format_cast_default(cast_formats)
            if (cast_default_null or cast_formats)
            else ""
        )

        statement = (
            f"CREATE INDEX {safe_ident(index_name)}"
            f"{' IF NOT EXISTS' if if_not_exists else ''} "
            f"ON {ks} ({field_clause})"
            f"{' EXCLUDE UNKNOWN KEY' if exclude_unknown_key else ''}"
            f"{cast_clause};"
        )

        logger.debug(f"Creating index {index_name!r} on {ks}")
        cluster = get_oi_cluster(ctx)
        cluster.execute_query(statement)
        logger.info(f"Created index {index_name!r} on {ks}")
        return tool_success(index_name=index_name, keyspace=ks, statement=statement)
    except Exception as e:
        logger.error(f"Error creating index {index_name!r} on {ks}: {e}", exc_info=True)
        return tool_error(e, index_name=index_name, keyspace=ks)
