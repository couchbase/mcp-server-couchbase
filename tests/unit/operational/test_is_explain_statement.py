"""Unit tests for _is_explain_statement() function.

Tests for:
- Detection of EXPLAIN statements with space after EXPLAIN
- Detection of EXPLAIN statements with newline after EXPLAIN
- Detection of EXPLAIN statements with tab after EXPLAIN
- Non-detection of non-EXPLAIN statements
"""

from cb_mcp.tools.operational.query import _is_explain_statement


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
        """Should detect EXPLAIN statements with comments after keyword."""
        assert (
            _is_explain_statement("EXPLAIN /* comment */ SELECT * FROM users") is True
        )

    def test_non_explain_with_explain_in_string(self) -> None:
        """Should not detect EXPLAIN when it's part of a string literal."""
        assert _is_explain_statement("SELECT * FROM explain WHERE ...") is False
