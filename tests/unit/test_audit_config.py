"""Tests for audit configuration resolution.

Coverage map:
- defaults when nothing is configured
- enabling without a file is reported and leaves auditing off, not fatal
- rotation size and backup count fallbacks on invalid input
- MB to bytes conversion
- disabled-events parsing: ids, names, files, comments, whitespace
- non-filterable events cannot be suppressed
- unknown events are ignored with a warning
- the tool-args warning fires only when it can matter
- the snapshot round-trips for the status tool
"""

from __future__ import annotations

import logging

import pytest

from cb_mcp.audit.config import (
    ResolvedAuditConfig,
    parse_disabled_events,
    resolve_audit_config,
)
from cb_mcp.utils.constants import BYTES_PER_MB


def _resolve(**overrides) -> ResolvedAuditConfig:
    options = {
        "enabled": None,
        "file": None,
        "rotation_max_size_mb": None,
        "retention_backup_count": None,
        "tool_args": None,
        "disabled_events": None,
    }
    options.update(overrides)
    return resolve_audit_config(**options)


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------


def test_defaults_are_off_and_match_the_prd():
    config = _resolve()
    assert config.enabled is False
    assert config.file is None
    assert config.process_file is None
    assert config.rotation_max_size_mb == 1.0
    assert config.retention_backup_count == 1000
    assert config.tool_args is False
    assert config.disabled_events == ()


def test_enabled_with_a_file_resolves_the_process_scoped_path(tmp_path):
    config = _resolve(enabled=True, file=str(tmp_path / "audit.log"))
    assert config.enabled is True
    assert config.process_file is not None
    assert config.process_file.endswith(".log")
    assert "audit." in config.process_file


def test_mb_is_converted_to_bytes(tmp_path):
    config = _resolve(
        enabled=True, file=str(tmp_path / "a.log"), rotation_max_size_mb=2.5
    )
    assert config.max_bytes == int(2.5 * BYTES_PER_MB)


# ---------------------------------------------------------------------------
# error and fallback handling
# ---------------------------------------------------------------------------


def test_enabled_without_a_file_is_reported_and_disables_auditing(caplog):
    with caplog.at_level(logging.ERROR):
        config = _resolve(enabled=True, file=None)
    # The PRD is explicit: the server must still start.
    assert config.enabled is False
    assert config.process_file is None
    assert "no audit file path is configured" in caplog.text


def test_blank_file_is_treated_as_absent(caplog):
    with caplog.at_level(logging.ERROR):
        config = _resolve(enabled=True, file="   ")
    assert config.enabled is False
    assert config.file is None


@pytest.mark.parametrize("bad_size", [0, 0.0, -5])
def test_invalid_rotation_size_falls_back_with_a_warning(tmp_path, caplog, bad_size):
    with caplog.at_level(logging.WARNING):
        config = _resolve(
            enabled=True,
            file=str(tmp_path / "a.log"),
            rotation_max_size_mb=bad_size,
        )
    assert config.rotation_max_size_mb == 1.0
    assert "falling back to the default" in caplog.text


def test_negative_backup_count_falls_back_with_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        config = _resolve(
            enabled=True, file=str(tmp_path / "a.log"), retention_backup_count=-3
        )
    assert config.retention_backup_count == 1000
    assert "retention backup count" in caplog.text


def test_zero_backup_count_is_honoured(tmp_path):
    config = _resolve(
        enabled=True, file=str(tmp_path / "a.log"), retention_backup_count=0
    )
    assert config.retention_backup_count == 0


def test_tool_args_warns_only_when_auditing_is_active(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        _resolve(enabled=True, file=str(tmp_path / "a.log"), tool_args=True)
    assert "recorded verbatim" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _resolve(enabled=False, tool_args=True)
    # Nothing is being written, so there is nothing to warn about.
    assert "recorded verbatim" not in caplog.text


# ---------------------------------------------------------------------------
# disabled events
# ---------------------------------------------------------------------------


def test_disabled_events_accepts_ids_and_names():
    assert parse_disabled_events("61490") == {61490}
    assert parse_disabled_events("document read") == {61490}
    assert parse_disabled_events("61490, query read") == {61490, 61491}


@pytest.mark.parametrize("raw", [None, "", "   ", ","])
def test_empty_disabled_events_is_empty(raw):
    assert parse_disabled_events(raw) == set()


def test_disabled_events_from_a_file_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / "filters.txt"
    path.write_text(
        "\n".join(
            [
                "# suppress the noisy reads",
                "document read",
                "",
                "   query read   ",
                "# trailing comment",
            ]
        ),
        encoding="utf-8",
    )
    assert parse_disabled_events(str(path)) == {61490, 61491}


def test_non_filterable_events_cannot_be_suppressed(caplog):
    with caplog.at_level(logging.WARNING):
        # A write id, a security event and a lifecycle event.
        result = parse_disabled_events("61522, scope check denied, server started")
    assert result == set()
    assert "Refused to disable non-filterable" in caplog.text


def test_unknown_events_are_ignored_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        result = parse_disabled_events("99999, not an event, document read")
    assert result == {61490}
    assert "unknown audit event" in caplog.text.lower()


def test_filters_are_not_parsed_when_auditing_is_disabled():
    config = _resolve(enabled=False, disabled_events="document read")
    assert config.disabled_events == ()


def test_session_initialized_is_filterable(tmp_path):
    config = _resolve(
        enabled=True,
        file=str(tmp_path / "a.log"),
        disabled_events="session initialized",
    )
    assert config.disabled_events == (57360,)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_as_dict_is_json_friendly_and_complete(tmp_path):
    config = _resolve(
        enabled=True,
        file=str(tmp_path / "audit.log"),
        rotation_max_size_mb=4,
        retention_backup_count=7,
        tool_args=True,
        disabled_events="document read",
    )
    snapshot = config.as_dict()
    assert snapshot == {
        "enabled": True,
        "file": str(tmp_path / "audit.log"),
        "process_file": config.process_file,
        "rotation_max_size_mb": 4.0,
        "retention_backup_count": 7,
        "tool_args": True,
        "disabled_events": [61490],
    }
