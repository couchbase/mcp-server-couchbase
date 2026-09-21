"""SQL++ identifier-quoting helpers shared by the Operational Insights tools.

SQL++ has no bind-parameter support for identifiers (only for values), so
database/scope/collection/index names must be safely quoted before being
interpolated into a statement. Backtick-quoting alone isn't enough — a name
containing a backtick could otherwise break out of the identifier and inject
arbitrary SQL++.
"""


def safe_ident(name: str) -> str:
    """Backtick-quote a SQL++ identifier, doubling embedded backticks."""
    return "`" + name.replace("`", "``") + "`"


def safe_field_path(path: str) -> str:
    """Backtick-quote each dot-separated segment of a nested field path."""
    return ".".join(safe_ident(segment) for segment in path.split("."))


def quote_literal(value: str) -> str:
    """Double-quote a SQL++ string literal, escaping backslashes and quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def keyspace(database_name: str, scope_name: str, collection_name: str) -> str:
    """Build a fully backtick-quoted three-part keyspace reference.

    Output must stay byte-identical to the historical
    ``f"{safe_ident(a)}.{safe_ident(b)}.{safe_ident(c)}"`` expression: callers
    (``create_index``) echo this value back to callers, and tests assert on
    the exact string.
    """
    return f"{safe_ident(database_name)}.{safe_ident(scope_name)}.{safe_ident(collection_name)}"
