"""Index management and introspection tools for Enterprise Analytics.

The ``couchbase-analytics`` SDK exposes no index manager at all. Index DDL
therefore has to go through SQL++ ``CREATE INDEX``, per the EA tool spec, and
listing indexes has to read the ``System.Metadata.Index`` catalog directly.
"""

import logging
from typing import Any

from couchbase_analytics.options import QueryOptions
from fastmcp import Context

from ..connection import get_cluster_connection
from ..responses import tool_error, tool_success
from .metadata import safe_ident

logger = logging.getLogger("ea-mcp-server.tools.index")


def safe_field_path(path: str) -> str:
    return ".".join(safe_ident(segment) for segment in path.split("."))


def _quote_literal(value: str) -> str:
    """Double-quote a SQL++ string literal, escaping backslashes and quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


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
            parts.append(f"{keyword.upper()} {_quote_literal(fmt)}")
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
    """Create a secondary index on an Enterprise Analytics collection via CREATE INDEX.

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
    created with exclude_unknown_key=True -- EA rejects an array index without
    it ("Array indexes must specify EXCLUDE UNKNOWN KEY."), and rejects
    INCLUDE UNKNOWN KEY on arrays too.

    Set cast_default_null=True to add CAST (DEFAULT NULL), which casts each
    value to the indexed type before indexing and stores NULL when the cast
    fails. Include it when the index supports a Tabular Analytics View (TAV).
    cast_formats gives non-ISO-8601 date/time formats and implies the clause,
    e.g. cast_formats={"date": "MM/DD/YYYY"} on a field indexed as date emits
    CAST (DEFAULT NULL DATE "MM/DD/YYYY"). Accepted keys: date, time, datetime.

    Returns {"success": True, "index_name": ..., "keyspace": ..., "statement": ...},
    or {"success": False, "error": ...} on failure.
    """
    keyspace = (
        f"{safe_ident(database_name)}.{safe_ident(scope_name)}."
        f"{safe_ident(collection_name)}"
    )

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
            f"ON {keyspace} ({field_clause})"
            f"{' EXCLUDE UNKNOWN KEY' if exclude_unknown_key else ''}"
            f"{cast_clause};"
        )

        logger.debug(f"Creating index {index_name!r} on {keyspace}")
        cluster = get_cluster_connection(ctx)
        cluster.execute_query(statement)
        logger.info(f"Created index {index_name!r} on {keyspace}")
        return tool_success(
            index_name=index_name, keyspace=keyspace, statement=statement
        )
    except Exception as e:
        logger.error(
            f"Error creating index {index_name!r} on {keyspace}: {e}", exc_info=True
        )
        return tool_error(e, index_name=index_name, keyspace=keyspace)


def list_indexes(
    ctx: Context,
    database_name: str | None = None,
    scope_name: str | None = None,
    collection_name: str | None = None,
) -> list[dict[str, Any]]:
    """List user-created secondary indexes on Enterprise Analytics collections.

    database_name, scope_name and collection_name are independent optional
    filters; with none given, every secondary index in the cluster is listed.
    Primary indexes, optimizer samples and internal System indexes are not
    listed, as none can be acted on.

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
    # catalog indexes); primary indexes, which in Analytics *are* the
    # collection itself rather than a separate index; and IndexStructure
    # "SAMPLE" rows, the samples the cost-based optimizer maintains via
    # ANALYZE COLLECTION (these are not primary, so IsPrimary misses them).
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
        cluster = get_cluster_connection(ctx)
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
