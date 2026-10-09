"""Tests for audit configuration resolution.

Coverage map:
- defaults when nothing is configured
- sink parsing: console, file, both, invalid, empty
- selecting the file sink without a path is reported and degrades, not fatal
- a path with no file sink is warned about
- rotation size: 0 is off, negative falls back
- rotation interval parsing: units, 0, invalid
- retention: 0 honoured, negative falls back
- MB to bytes conversion
- disabled-events parsing: ids only, files, comments, whitespace
- non-filterable events cannot be suppressed
- unknown events are ignored with a warning
- the tool-args warning fires only when it can matter
- the snapshot round-trips for the status tool
- one test per configuration case in the PRD
"""

from __future__ import annotations

import logging

import pytest

from cb_mcp.audit.config import (
    ResolvedAuditConfig,
    parse_audit_sinks,
    parse_disabled_events,
    parse_rotation_interval,
    resolve_audit_config,
)
from cb_mcp.utils.constants import BYTES_PER_MB

DAY = 86_400
WEEK = 604_800


def _resolve(**overrides) -> ResolvedAuditConfig:
    options = {
        "enabled": None,
        "sinks": None,
        "file": None,
        "rotation_max_size_mb": None,
        "rotation_interval": None,
        "max_backups": None,
        "tool_args": None,
        "disabled_events": None,
    }
    options.update(overrides)
    return resolve_audit_config(**options)


def _file_audit(tmp_path, **overrides) -> ResolvedAuditConfig:
    """An enabled, file-sinked configuration — the shape a deployment uses."""
    options = {
        "enabled": True,
        "sinks": "file",
        "file": str(tmp_path / "audit.log"),
    }
    options.update(overrides)
    return _resolve(**options)


# ---------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------


def test_defaults_are_off_and_match_the_prd():
    config = _resolve()
    assert config.enabled is False
    assert config.sinks == ("console",)
    assert config.file is None
    assert config.process_file is None
    assert config.rotation_max_size_mb == 10.0
    assert config.rotation_interval == "1d"
    assert config.rotation_interval_seconds == DAY
    assert config.max_backups == 10
    assert config.tool_args is False
    assert config.disabled_events == ()


def test_enabled_with_a_file_resolves_the_process_scoped_path(tmp_path):
    config = _file_audit(tmp_path)
    assert config.enabled is True
    assert config.writes_file is True
    assert config.writes_console is False
    assert config.process_file is not None
    assert config.process_file.endswith(".log")
    assert "audit." in config.process_file


def test_mb_is_converted_to_bytes(tmp_path):
    config = _file_audit(tmp_path, rotation_max_size_mb=2.5)
    assert config.max_bytes == int(2.5 * BYTES_PER_MB)


# ---------------------------------------------------------------------------
# sinks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("console", ("console",)),
        ("file", ("file",)),
        ("console,file", ("console", "file")),
        # Order, case and spacing are the operator's; the result is canonical.
        ("FILE, console", ("console", "file")),
        ("file,file", ("file",)),
    ],
)
def test_sinks_parse_like_the_logging_sinks(raw, expected):
    assert parse_audit_sinks(raw) == expected


def test_invalid_sink_tokens_are_warned_about_and_ignored(caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_audit_sinks("file,syslog") == ("file",)
    assert "invalid audit sink" in caplog.text.lower()


@pytest.mark.parametrize("raw", ["", "   ", ",", "nowhere"])
def test_no_valid_sink_falls_back_to_the_default(raw, caplog):
    """Enabling auditing must always produce output somewhere."""
    with caplog.at_level(logging.WARNING):
        assert parse_audit_sinks(raw) == ("console",)


def test_console_only_needs_no_file(caplog):
    with caplog.at_level(logging.ERROR):
        config = _resolve(enabled=True, sinks="console")
    assert config.enabled is True
    assert config.writes_console is True
    assert config.writes_file is False
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# error and fallback handling
# ---------------------------------------------------------------------------


def test_file_sink_without_a_path_disables_only_that_sink(caplog):
    """The PRD is explicit: the server must still start."""
    with caplog.at_level(logging.ERROR):
        config = _resolve(enabled=True, sinks="console,file", file=None)
    # Console was also selected, so records still go somewhere.
    assert config.enabled is True
    assert config.sinks == ("console",)
    assert config.process_file is None
    assert "no audit file path is configured" in caplog.text


def test_file_only_without_a_path_leaves_auditing_off(caplog):
    with caplog.at_level(logging.ERROR):
        config = _resolve(enabled=True, sinks="file", file=None)
    assert config.enabled is False
    assert config.sinks == ()
    assert "audit logging disabled" in caplog.text


def test_blank_file_is_treated_as_absent(caplog):
    with caplog.at_level(logging.ERROR):
        config = _resolve(enabled=True, sinks="file", file="   ")
    assert config.enabled is False
    assert config.file is None


def test_a_path_with_no_file_sink_is_warned_about(tmp_path, caplog):
    """The quiet mistake: a path is set, nothing is written to it."""
    with caplog.at_level(logging.WARNING):
        config = _resolve(
            enabled=True, sinks="console", file=str(tmp_path / "audit.log")
        )
    assert config.writes_file is False
    assert config.process_file is None
    assert "no audit file will be written" in caplog.text


def test_zero_rotation_size_turns_size_rotation_off(tmp_path, caplog):
    """0 is a documented instruction, not invalid input."""
    with caplog.at_level(logging.WARNING):
        config = _file_audit(tmp_path, rotation_max_size_mb=0)
    assert config.rotation_max_size_mb == 0
    assert config.max_bytes == 0
    assert "falling back" not in caplog.text


def test_negative_rotation_size_falls_back_with_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        config = _file_audit(tmp_path, rotation_max_size_mb=-5)
    assert config.rotation_max_size_mb == 10.0
    assert "falling back to the default" in caplog.text


def test_negative_max_backups_falls_back_with_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        config = _file_audit(tmp_path, max_backups=-3)
    assert config.max_backups == 10
    assert "max backups" in caplog.text


def test_zero_max_backups_is_honoured(tmp_path):
    assert _file_audit(tmp_path, max_backups=0).max_backups == 0


def test_tool_args_warns_only_when_auditing_is_active(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        _file_audit(tmp_path, tool_args=True)
    assert "recorded verbatim" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _resolve(enabled=False, tool_args=True)
    # Nothing is being written, so there is nothing to warn about.
    assert "recorded verbatim" not in caplog.text


# ---------------------------------------------------------------------------
# rotation interval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("1d", DAY),
        ("30d", 30 * DAY),
        ("1w", WEEK),
        ("8w", 8 * WEEK),
        # The PRD writes "1W" in one of its cases; case must not matter.
        ("1W", WEEK),
        ("  1d  ", DAY),
        ("0", 0),
        ("0d", 0),
    ],
)
def test_interval_parsing_accepts_the_prd_vocabulary(raw, seconds):
    assert parse_rotation_interval(raw) == seconds


@pytest.mark.parametrize(
    "raw",
    [
        "1h",  # hours are not a unit: the PRD defines d and w only
        "1m",
        "d",
        "1",  # a bare number other than 0 has no unit to apply
        "1d2h",
        "-1d",
        "one day",
        "",
    ],
)
def test_interval_parsing_rejects_everything_else(raw):
    assert parse_rotation_interval(raw) is None


def test_invalid_interval_falls_back_to_the_default_with_a_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        config = _file_audit(tmp_path, rotation_interval="1h")
    assert config.rotation_interval == "1d"
    assert config.rotation_interval_seconds == DAY
    assert "Invalid audit rotation interval" in caplog.text


def test_zero_interval_turns_interval_rotation_off(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        config = _file_audit(tmp_path, rotation_interval="0")
    assert config.rotation_interval == "0"
    assert config.rotation_interval_seconds == 0
    assert "Invalid" not in caplog.text


# ---------------------------------------------------------------------------
# disabled events
# ---------------------------------------------------------------------------


def test_disabled_events_accepts_numeric_ids():
    assert parse_disabled_events("61490") == {61490}
    assert parse_disabled_events("61490, 61491") == {61490, 61491}
    assert parse_disabled_events(" 61490 ,61491 ") == {61490, 61491}


def test_catalogue_names_are_not_accepted(caplog):
    """Names were accepted once and were withdrawn deliberately.

    The id is the wire contract; the name is prose. Names carry spaces and
    parentheses, which makes them awkward to quote in a shell or a Docker -e
    value, and a name may be reworded for clarity in a way an id never is — so
    a filter written against one could silently stop matching. A name is now
    treated as an unknown entry and warned about, rather than half-working.
    """
    with caplog.at_level(logging.WARNING):
        assert parse_disabled_events("document read") == set()
        assert parse_disabled_events("61490, query read") == {61490}
    assert "unknown audit event" in caplog.text.lower()


@pytest.mark.parametrize("raw", [None, "", "   ", ","])
def test_empty_disabled_events_is_empty(raw):
    assert parse_disabled_events(raw) == set()


def test_disabled_events_from_a_file_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / "filters.txt"
    path.write_text(
        "\n".join(
            [
                "# suppress the noisy reads",
                "61490",
                "",
                "   61491   ",
                "# trailing comment",
            ]
        ),
        encoding="utf-8",
    )
    assert parse_disabled_events(str(path)) == {61490, 61491}


def test_non_filterable_events_cannot_be_suppressed(caplog):
    with caplog.at_level(logging.WARNING):
        # A write id, a security event and a lifecycle event, all by id.
        result = parse_disabled_events("61522, 57377, 57344")
    assert result == set()
    assert "Refused to disable non-filterable" in caplog.text


def test_unknown_events_are_ignored_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING):
        result = parse_disabled_events("99999, not an event, 61490")
    assert result == {61490}
    assert "unknown audit event" in caplog.text.lower()


def test_filters_are_not_parsed_when_auditing_is_disabled():
    config = _resolve(enabled=False, disabled_events="61490")
    assert config.disabled_events == ()


def test_session_initialized_is_filterable(tmp_path):
    config = _file_audit(tmp_path, disabled_events="57360")
    assert config.disabled_events == (57360,)


# ---------------------------------------------------------------------------
# the PRD's configuration cases
# ---------------------------------------------------------------------------


def test_case_1_audit_logs_switched_off():
    config = _resolve(enabled=False)
    assert config.enabled is False
    assert config.writes_console is False
    assert config.writes_file is False


def test_case_2_single_file_with_no_limits(tmp_path):
    """'I don't care about the size. I want to store them all.'"""
    config = _file_audit(
        tmp_path, rotation_max_size_mb=0, rotation_interval="0", max_backups=0
    )
    assert config.max_bytes == 0
    assert config.rotation_interval_seconds == 0
    assert config.max_backups == 0


def test_case_3_single_file_with_limited_space(tmp_path):
    config = _file_audit(
        tmp_path, rotation_max_size_mb=10, rotation_interval="0", max_backups=0
    )
    assert config.max_bytes == 10 * BYTES_PER_MB
    assert config.rotation_interval_seconds == 0
    assert config.max_backups == 0


def test_case_4_limited_space_across_multiple_files(tmp_path):
    """10 MB a file and 9 backups: 100 MB in total, as the PRD computes it."""
    config = _file_audit(
        tmp_path, rotation_max_size_mb=10, rotation_interval="0", max_backups=9
    )
    assert config.max_bytes == 10 * BYTES_PER_MB
    assert config.rotation_interval_seconds == 0
    assert config.max_backups == 9
    assert config.max_bytes * (config.max_backups + 1) == 100 * BYTES_PER_MB


def test_case_5_ninety_days_of_daily_logs(tmp_path):
    config = _file_audit(
        tmp_path, rotation_max_size_mb=0, rotation_interval="1d", max_backups=90
    )
    assert config.max_bytes == 0
    assert config.rotation_interval_seconds == DAY
    assert config.max_backups == 90


def test_case_6_ten_weeks_of_weekly_logs(tmp_path):
    config = _file_audit(
        tmp_path, rotation_max_size_mb=0, rotation_interval="1W", max_backups=9
    )
    assert config.max_bytes == 0
    assert config.rotation_interval_seconds == WEEK
    assert config.max_backups == 9


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_as_dict_is_json_friendly_and_complete(tmp_path):
    config = _file_audit(
        tmp_path,
        sinks="console,file",
        rotation_max_size_mb=4,
        rotation_interval="2w",
        max_backups=7,
        tool_args=True,
        disabled_events="61490",
    )
    snapshot = config.as_dict()
    assert snapshot == {
        "enabled": True,
        "sinks": ["console", "file"],
        "file": str(tmp_path / "audit.log"),
        "process_file": config.process_file,
        "rotation_max_size_mb": 4.0,
        "rotation_interval": "2w",
        "max_backups": 7,
        "tool_args": True,
        "disabled_events": [61490],
    }
