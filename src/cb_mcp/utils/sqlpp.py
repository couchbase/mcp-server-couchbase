"""SQL++ text helpers, shared by every server that speaks SQL++.

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

import re
from dataclasses import dataclass


def safe_ident(name: str) -> str:
    """Backtick-quote a SQL++ identifier, doubling embedded backticks."""
    return "`" + name.replace("`", "``") + "`"


def safe_field_path(path: str) -> str:
    """Backtick-quote each dot-separated segment of a nested field path."""
    return ".".join(safe_ident(segment) for segment in path.split("."))


def quote_literal(value: str) -> str:
    """Double-quote a SQL++ string literal, escaping backslashes and quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class QueryPlaceholders:
    """Placeholders a SQL++ statement expects values for.

    ``named`` holds the ``$name`` placeholders. ``positional`` is the number
    of positional values the statement expects: the highest ``$1``-style
    index or the number of ``?`` tokens, whichever is larger.
    """

    named: frozenset[str]
    positional: int

    def __bool__(self) -> bool:
        """True when the statement is parameterized at all."""
        return bool(self.named) or self.positional > 0


_PLACEHOLDER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLACEHOLDER_INDEX = re.compile(r"[0-9]+")


def find_query_placeholders(statement: str) -> QueryPlaceholders:
    """Find the parameter placeholders in a SQL++ statement.

    Scans the statement text rather than parsing it. String literals,
    backtick-quoted identifiers and both comment forms are skipped, so a
    ``$`` or ``?`` inside one is not read as a placeholder. Detection errs
    towards under-reporting.
    """
    named: set[str] = set()
    highest_index = 0
    question_marks = 0

    i = 0
    length = len(statement)
    while i < length:
        char = statement[i]

        if char in "'\"":
            i = _skip_quoted(statement, i, char, backslash_escapes=True)
            continue
        if char == "`":
            i = _skip_quoted(statement, i, char, backslash_escapes=False)
            continue
        if statement.startswith("--", i):
            newline = statement.find("\n", i)
            i = length if newline == -1 else newline + 1
            continue
        if statement.startswith("/*", i):
            end = statement.find("*/", i + 2)
            i = length if end == -1 else end + 2
            continue
        if char == "?":
            question_marks += 1
            i += 1
            continue
        if char == "$":
            index_match = _PLACEHOLDER_INDEX.match(statement, i + 1)
            if index_match:
                highest_index = max(highest_index, int(index_match.group()))
                i = index_match.end()
                continue
            name_match = _PLACEHOLDER_NAME.match(statement, i + 1)
            if name_match:
                named.add(name_match.group())
                i = name_match.end()
                continue

        i += 1

    return QueryPlaceholders(
        named=frozenset(named),
        positional=max(highest_index, question_marks),
    )


def _skip_quoted(
    statement: str, start: int, delimiter: str, *, backslash_escapes: bool
) -> int:
    """Return the index just past the quoted run opening at ``start``.

    Handles doubled-delimiter escapes and, for string literals, backslash
    escapes. An unterminated run consumes the rest of the statement.
    """
    i = start + 1
    length = len(statement)
    while i < length:
        char = statement[i]
        if backslash_escapes and char == "\\":
            i += 2
            continue
        if char == delimiter:
            if i + 1 < length and statement[i + 1] == delimiter:
                i += 2
                continue
            return i + 1
        i += 1
    return length
