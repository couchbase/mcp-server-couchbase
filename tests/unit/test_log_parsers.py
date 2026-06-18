"""Tests for parse_log_level and parse_log_sinks in cb_mcp.utils.logging.

These are the framework-agnostic parsers behind the Click validators. Both
follow a "lenient fallback" contract: invalid input falls back to defaults
and the original token is returned so callers can surface it later via the
configured logger.
"""

from __future__ import annotations

from cb_mcp.utils.constants import DEFAULT_LOG_LEVEL, DEFAULT_LOG_SINKS
from cb_mcp.utils.logging import parse_log_level, parse_log_sinks


class TestParseLogLevel:
    """parse_log_level resolves a level token, falling back on invalid input."""

    def test_valid_uppercase_passes_through(self):
        assert parse_log_level("INFO") == ("INFO", None)

    def test_valid_lowercase_normalised_to_uppercase(self):
        assert parse_log_level("debug") == ("DEBUG", None)

    def test_valid_mixedcase_normalised(self):
        assert parse_log_level("WaRnInG") == ("WARNING", None)

    def test_off_is_valid(self):
        assert parse_log_level("OFF") == ("OFF", None)

    def test_whitespace_trimmed_before_validation(self):
        assert parse_log_level("  info  ") == ("INFO", None)

    def test_invalid_falls_back_to_default_and_preserves_input(self):
        resolved, invalid = parse_log_level("BOGUS")
        assert resolved == DEFAULT_LOG_LEVEL
        assert invalid == "BOGUS"

    def test_invalid_preserves_original_casing(self):
        _, invalid = parse_log_level("Verbose")
        assert invalid == "Verbose"

    def test_empty_string_treated_as_invalid(self):
        resolved, invalid = parse_log_level("")
        assert resolved == DEFAULT_LOG_LEVEL
        # Empty string is the "invalid token"; caller can surface it as `repr("")`.
        assert invalid == ""


class TestParseLogSinks:
    """parse_log_sinks keeps valid tokens, collects invalid ones, and falls back."""

    def test_single_valid_token(self):
        sinks, invalid = parse_log_sinks("stderr")
        assert sinks == {"stderr"}
        assert invalid == []

    def test_multiple_valid_comma_separated(self):
        sinks, invalid = parse_log_sinks("stderr,file")
        assert sinks == {"stderr", "file"}
        assert invalid == []

    def test_case_insensitive_normalisation(self):
        sinks, invalid = parse_log_sinks("STDERR,File")
        assert sinks == {"stderr", "file"}
        assert invalid == []

    def test_whitespace_around_tokens(self):
        sinks, invalid = parse_log_sinks(" stderr , file ")
        assert sinks == {"stderr", "file"}
        assert invalid == []

    def test_empty_tokens_skipped(self):
        sinks, invalid = parse_log_sinks("stderr,,file,,,")
        assert sinks == {"stderr", "file"}
        assert invalid == []

    def test_mixed_valid_and_invalid(self):
        sinks, invalid = parse_log_sinks("stderr,foo")
        assert sinks == {"stderr"}
        assert invalid == ["foo"]

    def test_invalid_preserves_original_casing(self):
        _, invalid = parse_log_sinks("stderr,FoO")
        assert invalid == ["FoO"]

    def test_all_invalid_falls_back_to_default(self):
        sinks, invalid = parse_log_sinks("foo,bar")
        assert sinks == {DEFAULT_LOG_SINKS}
        assert invalid == ["foo", "bar"]

    def test_empty_string_falls_back_to_default(self):
        sinks, invalid = parse_log_sinks("")
        assert sinks == {DEFAULT_LOG_SINKS}
        assert invalid == []

    def test_only_whitespace_falls_back_to_default(self):
        sinks, invalid = parse_log_sinks("   ,  ,")
        assert sinks == {DEFAULT_LOG_SINKS}
        assert invalid == []
