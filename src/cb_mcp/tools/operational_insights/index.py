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

from couchbase_operational_insights.options import QueryOptions
from fastmcp import Context

from ...servers.operational_insights.constants import (
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)
from ...utils.operational_insights.context import get_oi_cluster
from ...utils.operational_insights.sqlpp import keyspace
from ...utils.responses import tool_error, tool_success
from ...utils.sqlpp import quote_literal, safe_field_path, safe_ident

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


#: The type grammar this tool's docstring documents (bigint, int, double,
#: string, date, time, datetime). Every "type" value below is interpolated
#: as raw SQL++, not a quoted literal or a backtick-quoted identifier — the
#: sqlpp helpers have nothing that escapes it — so it must be checked
#: against this closed set before interpolation instead.
_VALID_INDEX_TYPES = frozenset(
    {"bigint", "int", "double", "string", "date", "time", "datetime"}
)


def _safe_type(field_type: str) -> str:
    """Validate a field's declared type against the documented grammar.

    Raises ValueError, which create_index's caller catches and turns into a
    clean tool_error — same as any other malformed-input failure here.
    """
    if field_type not in _VALID_INDEX_TYPES:
        raise ValueError(
            f"Invalid index type {field_type!r}; must be one of "
            f"{sorted(_VALID_INDEX_TYPES)}"
        )
    return field_type


def _format_element(field: dict[str, Any]) -> str:
    """Render one IndexElement: "UNNEST ..." if it has 'unnest', else "path: type"."""
    if "unnest" not in field:
        name = safe_field_path(field["name"])
        field_type = field.get("type")
        return f"{name}: {_safe_type(field_type)}" if field_type else name

    unnest = field["unnest"]
    paths = [unnest] if isinstance(unnest, str) else unnest
    clause = " ".join(f"UNNEST {safe_field_path(p)}" for p in paths)

    select = field.get("select")
    if select is None:
        return f"{clause}: {_safe_type(field['type'])}"
    return f"{clause} SELECT " + ", ".join(
        f"{safe_field_path(s['name'])}: {_safe_type(s['type'])}" for s in select
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


def list_indexes(
    ctx: Context,
    database_name: str | None = None,
    scope_name: str | None = None,
    collection_name: str | None = None,
) -> list[dict[str, Any]]:
    """List user-created secondary indexes on Operational Insights collections.

    database_name, scope_name and collection_name are independent optional
    filters; with none given, every secondary index in the cluster is listed.
    Primary indexes, optimizer samples and internal System indexes are not
    listed, as none can be acted on.

    Note: this server and the operational server both expose a tool named
    ``list_indexes``. They run as separate processes/servers, so this is only
    a concern for a client that registers both simultaneously — see
    CONTRIBUTING.md's tool-naming section.

    Returns a list of rows with DatabaseName, ScopeName, CollectionName,
    IndexName, IndexStructure and ExcludeUnknownKey. The indexed fields are
    under SearchKey for scalar indexes, or SearchKeyElements (UnnestList /
    ProjectList) for array indexes, which leave SearchKey empty.
    Each field path is an array of path components,
    so ["ratings", "Lyrics"] means ratings.Lyrics.
    """
    named_parameters = {}
    if database_name:
        named_parameters["DatabaseName"] = database_name
    if scope_name:
        named_parameters["DataverseName"] = scope_name
    if collection_name:
        named_parameters["DatasetName"] = collection_name

    # Three classes of Metadata.`Index` row are excluded, since none is a
    # user-created secondary index: rows in the System database (internal
    # catalog indexes); primary indexes, which are the collection itself
    # rather than a separate index; and IndexStructure "SAMPLE" rows, the
    # samples the cost-based optimizer maintains via ANALYZE COLLECTION
    # (these are not primary, so IsPrimary misses them).
    query = (
        "SELECT i.DatabaseName, "
        "i.DataverseName AS ScopeName, "
        "i.DatasetName AS CollectionName, "
        "i.IndexName, "
        "i.IndexStructure, "
        "i.SearchKey, "
        "i.SearchKeyElements, "
        "i.ExcludeUnknownKey "
        "FROM System.Metadata.`Index` i "
        'WHERE i.DatabaseName <> "System" '
        "AND i.IsPrimary = false "
        'AND i.IndexStructure <> "SAMPLE" '
        + "".join(f"AND i.{field} = ${field} " for field in named_parameters)
        + "ORDER BY i.DatabaseName, ScopeName, CollectionName, i.IndexName;"
    )

    target = ", ".join(
        f"{label}={value}"
        for label, value in (
            ("database", database_name),
            ("scope", scope_name),
            ("collection", collection_name),
        )
        if value
    )
    target = target or "the whole cluster"
    try:
        logger.debug(f"Listing secondary indexes for {target}")
        cluster = get_oi_cluster(ctx)
        result = cluster.execute_query(
            query, QueryOptions(named_parameters=named_parameters)
        )
        rows = result.get_all_rows()
        logger.info(f"Found {len(rows)} secondary index(es) for {target}")
        return rows
    except Exception as e:
        logger.error(
            f"Error listing secondary indexes for {target}: {e}", exc_info=True
        )
        raise
