"""Unit tests for _is_explain_statement() function.

Detection is grammar-based: the query is parsed with ``lark_sqlpp`` and the
AST is checked for an ``explain_statement`` node, with a lexical prefix check
as a fallback for input the grammar cannot parse (e.g. comments).

Tests for:
- Detection of EXPLAIN statements with various whitespace after EXPLAIN
- Detection of EXPLAIN regardless of case
- Detection of EXPLAIN wrapping write statements (UPDATE/DELETE/CREATE)
- Non-detection of non-EXPLAIN statements
- The lexical fallback path for queries the grammar cannot parse
"""

from cb_mcp.tools.query import _is_explain_statement


class TestIsExplainStatement:
    """Unit tests for _is_explain_statement() function."""

    def test_explain_with_space(self) -> None:
        """Should detect EXPLAIN statements with space after EXPLAIN."""
        assert _is_explain_statement("EXPLAIN SELECT * FROM users") is True

    def test_explain_with_newline(self) -> None:
        """Should detect EXPLAIN statements with newline after EXPLAIN.

        This tests the bug fix for multi-line queries.
        """
        assert _is_explain_statement("EXPLAIN\nSELECT * FROM users") is True

    def test_explain_with_tab(self) -> None:
        """Should detect EXPLAIN statements with tab after EXPLAIN."""
        assert _is_explain_statement("EXPLAIN\tSELECT * FROM users") is True

    def test_explain_with_carriage_return_newline(self) -> None:
        """Should detect EXPLAIN statements with CRLF after EXPLAIN."""
        assert _is_explain_statement("EXPLAIN\r\nSELECT * FROM users") is True

    def test_explain_lowercase(self) -> None:
        """Should detect EXPLAIN statements regardless of case."""
        assert _is_explain_statement("explain select * from users") is True

    def test_explain_mixed_case(self) -> None:
        """Should detect EXPLAIN statements with mixed case."""
        assert _is_explain_statement("ExPlAiN SELECT * FROM users") is True

    def test_explain_with_leading_whitespace(self) -> None:
        """Should detect EXPLAIN statements with leading whitespace."""
        assert _is_explain_statement("  EXPLAIN SELECT * FROM users") is True
        assert _is_explain_statement("\nEXPLAIN SELECT * FROM users") is True
        assert _is_explain_statement("\tEXPLAIN SELECT * FROM users") is True

    def test_explain_with_leading_whitespace_and_newline(self) -> None:
        """Should detect EXPLAIN statements with leading whitespace and newline after EXPLAIN."""
        assert _is_explain_statement("  EXPLAIN\nSELECT * FROM users") is True
        assert _is_explain_statement("\tEXPLAIN\tSELECT * FROM users") is True

    def test_non_explain_select(self) -> None:
        """Should not detect non-EXPLAIN SELECT statements."""
        assert _is_explain_statement("SELECT * FROM users") is False

    def test_non_explain_insert(self) -> None:
        """Should not detect non-EXPLAIN INSERT statements."""
        assert _is_explain_statement("INSERT INTO users VALUES (...)") is False

    def test_non_explain_update(self) -> None:
        """Should not detect non-EXPLAIN UPDATE statements."""
        assert _is_explain_statement("UPDATE users SET age = 25") is False

    def test_non_explain_delete(self) -> None:
        """Should not detect non-EXPLAIN DELETE statements."""
        assert _is_explain_statement("DELETE FROM users WHERE age < 18") is False

    def test_explain_without_space_after(self) -> None:
        """Should not detect EXPLAIN without whitespace after it (incomplete statement)."""
        assert _is_explain_statement("EXPLAIN") is False

    def test_explain_with_comment_after_explain(self) -> None:
        """Should detect EXPLAIN statements with comments after keyword.

        The grammar does not model comments, so this exercises the lexical
        fallback path.
        """
        assert (
            _is_explain_statement("EXPLAIN /* comment */ SELECT * FROM users") is True
        )

    def test_non_explain_with_explain_in_string(self) -> None:
        """Should not detect EXPLAIN when it's part of a string literal."""
        assert _is_explain_statement("SELECT * FROM explain WHERE ...") is False

    def test_explain_wrapping_update(self) -> None:
        """Should detect EXPLAIN even when it wraps a write statement.

        The grammar parses ``EXPLAIN UPDATE ...`` as an explain_statement
        wrapping a DML statement; it must still be classified as an EXPLAIN so
        the write-check is correctly bypassed (an EXPLAIN never mutates data).
        """
        assert _is_explain_statement("EXPLAIN UPDATE users SET age = 25") is True

    def test_explain_wrapping_delete(self) -> None:
        """Should detect EXPLAIN that wraps a DELETE statement."""
        assert _is_explain_statement("EXPLAIN DELETE FROM users WHERE age < 18") is True

    def test_non_explain_with_explain_as_identifier(self) -> None:
        """Should not detect EXPLAIN used as an ordinary identifier.

        Grammar-based detection is not fooled by ``explain`` appearing as a
        column/keyspace name rather than the leading statement keyword.
        """
        assert _is_explain_statement("SELECT explain FROM users") is False
