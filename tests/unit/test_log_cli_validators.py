"""Tests for the Click callbacks in cb_mcp.utils.cli.

These thin adapters wrap the parsers in :mod:`cb_mcp.utils.logging` and add
Click-specific behaviour (loud rejection for empty paths via ``BadParameter``).
The level/sink validators just forward, so testing focuses on the contract
(callbacks accept Click's ``(ctx, param, value)`` triplet) plus the loud
rejection behaviour for ``validate_log_path``.
"""

from __future__ import annotations

import click
import pytest

from cb_mcp.utils.cli import (
    validate_log_level,
    validate_log_path,
    validate_log_sinks,
)


class TestValidateLogLevel:
    """Click callback for --log-level. Delegates to parse_log_level.

    These tests invoke the validators directly (without going through a real
    Click invocation) and pass ``None`` or sentinel values for ``ctx`` /
    ``param`` because the callbacks document those args as unused.
    """

    def test_valid_level_returns_tuple_with_none_invalid(self):
        resolved, invalid = validate_log_level(None, None, "DEBUG")  # type: ignore[arg-type]
        assert resolved == "DEBUG"
        assert invalid is None

    def test_invalid_level_returns_default_plus_original_token(self):
        resolved, invalid = validate_log_level(None, None, "BOGUS")  # type: ignore[arg-type]
        # Invalid input falls back to DEFAULT_LOG_LEVEL ("INFO") and returns the
        # original token so configure_logging can surface an error record.
        assert resolved == "INFO"
        assert invalid == "BOGUS"

    def test_ignores_ctx_and_param_arguments(self):
        """The callback's ctx/param parameters are part of Click's contract but unused."""
        # Pass a sentinel; result must match the ctx=None case.
        result = validate_log_level("sentinel-ctx", "sentinel-param", "INFO")  # type: ignore[arg-type]
        assert result == ("INFO", None)


class TestValidateLogSinks:
    """Click callback for --log-sinks. Delegates to parse_log_sinks."""

    def test_valid_sinks(self):
        sinks, invalid = validate_log_sinks(None, None, "stderr,file")  # type: ignore[arg-type]
        assert sinks == {"stderr", "file"}
        assert invalid == []

    def test_invalid_token_collected(self):
        sinks, invalid = validate_log_sinks(None, None, "stderr,bogus")  # type: ignore[arg-type]
        assert sinks == {"stderr"}
        assert invalid == ["bogus"]

    def test_ignores_ctx_and_param_arguments(self):
        result = validate_log_sinks("sentinel-ctx", "sentinel-param", "stderr")  # type: ignore[arg-type]
        assert result == ({"stderr"}, [])


class TestValidateLogPath:
    """Click callback for --log-file (per-level base path). Loudly rejects empty."""

    def test_passes_non_empty_path_through(self):
        assert validate_log_path(None, None, "/tmp/foo.log") == "/tmp/foo.log"  # type: ignore[arg-type]

    def test_strips_surrounding_whitespace(self):
        assert validate_log_path(None, None, "  /tmp/foo.log  ") == "/tmp/foo.log"  # type: ignore[arg-type]

    def test_empty_string_raises_bad_parameter(self):
        with pytest.raises(click.BadParameter, match="path cannot be empty"):
            validate_log_path(None, None, "")  # type: ignore[arg-type]

    def test_whitespace_only_raises_bad_parameter(self):
        with pytest.raises(click.BadParameter, match="path cannot be empty"):
            validate_log_path(None, None, "   ")  # type: ignore[arg-type]

    def test_none_value_raises_bad_parameter(self):
        """Defensive: if Click somehow passes None (shouldn't with a default), reject."""
        with pytest.raises(click.BadParameter):
            validate_log_path(None, None, None)  # type: ignore[arg-type]
