"""SQL++ quoting helpers, shared by every server that speaks SQL++.

SQL++ has no bind-parameter support for *identifiers* (only for values), so
a database/bucket/scope/collection/index/field name reaching a statement has
to be quoted before interpolation. Backtick-quoting alone isn't enough — a
name containing a backtick would otherwise break out of the identifier and
inject arbitrary SQL++.

Shared rather than per-server because the quoting rules are a property of
the SQL++ grammar, not of a dialect: the operational server's SQL++ and the
Operational Insights server's Analytics SQL++ escape identifiers and string
literals identically.

Deliberately *not* here: anything that assembles a keyspace. Those differ —
the operational server's ``format_keyspace`` builds an unquoted
``bucket.scope.collection`` for log context only, while Operational
Insights' ``keyspace`` builds a backtick-quoted three-part reference for
statements. Merging them on the strength of a similar shape would produce a
helper whose output is safe for one caller and not the other.
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
