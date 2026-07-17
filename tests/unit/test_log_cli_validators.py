"""Tests for the Click callbacks in cb_mcp.utils.cli.

These thin adapters wrap the parsers in :mod:`cb_mcp.utils.logging` and add
Click-specific behaviour (loud rejection for empty paths via ``BadParameter``).
The level/sink validators just forward, so testing focuses on the contract
(callbacks accept Click's ``(ctx, param, value)`` triplet) plus the loud
rejection behaviour for ``validate_log_path``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import click
import pytest

from cb_mcp.utils.cli import (
    validate_log_level,
    validate_log_path,
    validate_log_sinks,
    validate_scope_label,
)
from cb_mcp.utils.constants import SCOPE_READ


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


def _scope_param(default: str = SCOPE_READ):
    """A minimal stand-in for the Click Option the callback reads.

    ``validate_scope_label`` only touches ``param.default`` (the fallback
    scope) and ``param.opts`` (the flag name, used in the warning), so a
    SimpleNamespace suffices without constructing a full Click invocation.
    """
    return SimpleNamespace(
        default=default, opts=["--oauth-scope-read-label"], name="oauth_scope_read"
    )


class TestValidateScopeLabel:
    """Click callback for the OAuth scope-label options.

    Returns a usable label unchanged (trimmed); warns and falls back to the
    option's default (the canonical scope) for blank/non-string input. Unlike
    validate_log_path, an unusable label is non-fatal — the server stays
    functional on the default — so it warns rather than raising.
    """

    def test_valid_label_passes_through(self):
        label = "couchbase-mcp/read"
        assert validate_scope_label(None, _scope_param(), label) == label  # type: ignore[arg-type]

    def test_strips_surrounding_whitespace(self):
        assert (
            validate_scope_label(None, _scope_param(), "  couchbase-mcp/read  ")  # type: ignore[arg-type]
            == "couchbase-mcp/read"
        )

    def test_default_passes_through_without_warning(self):
        """When the flag is omitted, Click passes the default (a valid str);
        the callback must return it without emitting a warning."""
        with patch("cb_mcp.utils.cli.logger") as mock_logger:
            result = validate_scope_label(None, _scope_param(SCOPE_READ), SCOPE_READ)  # type: ignore[arg-type]
        assert result == SCOPE_READ
        mock_logger.warning.assert_not_called()

    def test_empty_string_warns_and_falls_back(self):
        with patch("cb_mcp.utils.cli.logger") as mock_logger:
            result = validate_scope_label(None, _scope_param(SCOPE_READ), "")  # type: ignore[arg-type]
        assert result == SCOPE_READ
        mock_logger.warning.assert_called_once()

    def test_whitespace_only_warns_and_falls_back(self):
        with patch("cb_mcp.utils.cli.logger") as mock_logger:
            result = validate_scope_label(None, _scope_param(SCOPE_READ), "   ")  # type: ignore[arg-type]
        assert result == SCOPE_READ
        mock_logger.warning.assert_called_once()

    def test_non_string_value_warns_and_falls_back(self):
        """Defensive: a non-str value (e.g. from a programmatic caller) warns
        and falls back rather than propagating a type error downstream."""
        with patch("cb_mcp.utils.cli.logger") as mock_logger:
            result = validate_scope_label(None, _scope_param(SCOPE_READ), 123)  # type: ignore[arg-type]
        assert result == SCOPE_READ
        mock_logger.warning.assert_called_once()

    def test_none_value_warns_and_falls_back(self):
        with patch("cb_mcp.utils.cli.logger") as mock_logger:
            result = validate_scope_label(None, _scope_param(SCOPE_READ), None)  # type: ignore[arg-type]
        assert result == SCOPE_READ
        mock_logger.warning.assert_called_once()
