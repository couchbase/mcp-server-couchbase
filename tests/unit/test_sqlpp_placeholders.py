"""Unit tests for the SQL++ placeholder scanner in ``utils/sqlpp.py``.

The scanner decides whether ``explain_sql_plus_plus_query`` warns that a plan
was built without parameter values, so the cases that matter most are the ones
where a ``$`` or ``?`` is *not* a placeholder: inside a string literal, inside
a backtick-quoted identifier, or inside a comment. A false positive there would
tell the caller to bind a parameter that does not exist.
"""

from __future__ import annotations

from cb_mcp.utils.sqlpp import find_query_placeholders


class TestNoPlaceholders:
    """Statements without parameters must scan clean."""

    def test_plain_select(self) -> None:
        found = find_query_placeholders("SELECT * FROM users WHERE age > 18")

        assert found.named == frozenset()
        assert found.positional == 0
        assert not found

    def test_empty_statement(self) -> None:
        found = find_query_placeholders("")

        assert found.named == frozenset()
        assert found.positional == 0


class TestNamedPlaceholders:
    """``$name`` placeholders."""

    def test_collects_each_distinct_name(self) -> None:
        found = find_query_placeholders(
            "SELECT * FROM hotel WHERE country = $country AND city = $city"
        )

        assert found.named == {"country", "city"}
        assert found.positional == 0
        assert bool(found) is True

    def test_repeated_name_counted_once(self) -> None:
        found = find_query_placeholders(
            "SELECT * FROM t WHERE a = $value OR b = $value"
        )

        assert found.named == {"value"}

    def test_name_may_contain_underscores_and_digits(self) -> None:
        found = find_query_placeholders("SELECT * FROM t WHERE id = $user_id2")

        assert found.named == {"user_id2"}

    def test_bare_dollar_is_not_a_placeholder(self) -> None:
        """A `$` with nothing bindable after it must not invent a parameter."""
        found = find_query_placeholders("SELECT * FROM t WHERE a = $ AND b = 1")

        assert found.named == frozenset()
        assert found.positional == 0


class TestPositionalPlaceholders:
    """``$1`` and ``?`` placeholders."""

    def test_dollar_index_reports_highest_index(self) -> None:
        found = find_query_placeholders(
            "SELECT * FROM t WHERE a = $2 AND b = $1 AND c = $2"
        )

        assert found.named == frozenset()
        assert found.positional == 2

    def test_question_marks_are_counted(self) -> None:
        found = find_query_placeholders(
            "SELECT * FROM t WHERE a = ? AND b = ? AND c = ?"
        )

        assert found.positional == 3

    def test_mixed_forms_take_the_larger_requirement(self) -> None:
        """Pathological, but the server still looks for the larger count."""
        found = find_query_placeholders("SELECT * FROM t WHERE a = $3 AND b = ?")

        assert found.positional == 3

    def test_named_and_positional_together(self) -> None:
        found = find_query_placeholders("SELECT * FROM t WHERE a = $country AND b = ?")

        assert found.named == {"country"}
        assert found.positional == 1


class TestQuotingAndComments:
    """A `$` or `?` that is not code must be ignored."""

    def test_single_quoted_literal_is_skipped(self) -> None:
        found = find_query_placeholders(
            "SELECT * FROM t WHERE note = 'costs $amount?' AND a = $real"
        )

        assert found.named == {"real"}
        assert found.positional == 0

    def test_double_quoted_literal_is_skipped(self) -> None:
        found = find_query_placeholders(
            'SELECT * FROM t WHERE note = "costs $amount?" AND a = $real'
        )

        assert found.named == {"real"}
        assert found.positional == 0

    def test_doubled_quote_escape_does_not_end_the_literal(self) -> None:
        found = find_query_placeholders(
            "SELECT * FROM t WHERE note = 'it''s $hidden' AND a = $real"
        )

        assert found.named == {"real"}

    def test_backslash_escape_does_not_end_the_literal(self) -> None:
        found = find_query_placeholders(
            'SELECT * FROM t WHERE note = "quote \\" $hidden" AND a = $real'
        )

        assert found.named == {"real"}

    def test_backtick_identifier_is_skipped(self) -> None:
        found = find_query_placeholders("SELECT `col$hidden` FROM t WHERE a = $real")

        assert found.named == {"real"}

    def test_doubled_backtick_does_not_end_the_identifier(self) -> None:
        found = find_query_placeholders(
            "SELECT `odd``name$hidden` FROM t WHERE a = $real"
        )

        assert found.named == {"real"}

    def test_line_comment_is_skipped(self) -> None:
        found = find_query_placeholders("SELECT * FROM t -- $hidden ?\nWHERE a = $real")

        assert found.named == {"real"}
        assert found.positional == 0

    def test_block_comment_is_skipped(self) -> None:
        found = find_query_placeholders(
            "SELECT * FROM t /* $hidden\n ? */ WHERE a = $real"
        )

        assert found.named == {"real"}
        assert found.positional == 0

    def test_unterminated_literal_consumes_the_rest(self) -> None:
        """An unbalanced quote must not make the tail scan as code."""
        found = find_query_placeholders("SELECT * FROM t WHERE a = 'oops $hidden")

        assert found.named == frozenset()

    def test_unterminated_block_comment_consumes_the_rest(self) -> None:
        found = find_query_placeholders("SELECT * FROM t /* $hidden")

        assert found.named == frozenset()
