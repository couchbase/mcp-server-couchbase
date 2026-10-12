"""Tests for the shared query result budget.

The behaviour these pin down, in order of how much it would cost to get wrong:

1. A complete result is never reported as truncated. The budget is a ceiling,
   and a result that lands exactly on it is still complete — an off-by-one here
   would make every evenly-dividing query claim to be missing rows.
2. A truncated result always says so. The inverse failure is worse: a model
   presenting partial data as whole.
3. Collection is lazy. The point of the feature is that the remaining rows are
   never read, so a test that only checks the returned list would pass on an
   implementation that buffered everything first.
"""

import json
import logging
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from cb_mcp.utils.constants import (
    DEFAULT_MAX_QUERY_RESULT_SIZE,
    MAX_MAX_QUERY_RESULT_SIZE,
    MIN_MAX_QUERY_RESULT_SIZE,
)
from cb_mcp.utils.query_limits import (
    clamp_max_query_result_size,
    collect_rows_within_budget,
    max_query_result_size_for,
    max_query_result_size_from,
)

ROW = {
    "callsign": "MILE-AIR",
    "country": "United States",
    "id": 10,
    "name": "40-Mile Air",
}
ROW_BYTES = len(json.dumps(ROW).encode("utf-8"))


def _collect(rows, limit_bytes):
    return collect_rows_within_budget(rows, limit_bytes=limit_bytes, service="test")


class TestCompleteResults:
    """A result that fits must never be flagged as truncated."""

    def test_well_under_budget(self) -> None:
        bounded = _collect([ROW] * 3, 10_000)
        assert bounded.rows == [ROW] * 3
        assert bounded.truncated is False
        assert bounded.bytes_returned == ROW_BYTES * 3

    def test_empty_result(self) -> None:
        bounded = _collect([], 10_000)
        assert bounded.rows == []
        assert bounded.row_count == 0
        assert bounded.truncated is False

    def test_exact_fit_is_not_truncated(self) -> None:
        """The regression this guards: landing exactly on the limit is complete.

        Reporting truncation here would fire on any query whose rows divide
        evenly into the budget, which is common enough to be noticed.
        """
        bounded = _collect([ROW] * 3, ROW_BYTES * 3)
        assert bounded.row_count == 3
        assert bounded.truncated is False

    def test_complete_result_has_no_truncation_message(self) -> None:
        bounded = _collect([ROW], 10_000)
        assert bounded.truncation_message() is None
        assert bounded.as_envelope_fields() == {"truncated": False}


class TestTruncation:
    def test_stops_at_the_budget(self) -> None:
        bounded = _collect([ROW] * 100, ROW_BYTES * 3)
        assert bounded.row_count == 3
        assert bounded.truncated is True
        assert bounded.bytes_returned <= ROW_BYTES * 3

    def test_exact_fit_with_more_rows_is_truncated(self) -> None:
        """Same byte total as test_exact_fit_is_not_truncated, different verdict.

        The pair is the whole point: the flag depends on whether the stream had
        anything left, not on the byte count.
        """
        bounded = _collect([ROW] * 4, ROW_BYTES * 3)
        assert bounded.row_count == 3
        assert bounded.truncated is True

    def test_envelope_fields_carry_the_counts(self) -> None:
        bounded = _collect([ROW] * 100, ROW_BYTES * 2)
        fields = bounded.as_envelope_fields()
        assert fields["truncated"] is True
        assert fields["truncation"]["limit_bytes"] == ROW_BYTES * 2
        assert fields["truncation"]["bytes_returned"] == bounded.bytes_returned
        assert "narrow the query" in fields["truncation"]["message"]

    def test_message_says_the_rows_are_unrecoverable(self) -> None:
        """The model must not go looking for a cursor: this is not pagination."""
        message = _collect([ROW] * 100, ROW_BYTES * 2).truncation_message()
        assert "cannot be retrieved by calling again" in message

    def test_warns_once_when_truncating(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            _collect([ROW] * 100, ROW_BYTES * 2)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "truncated" in warnings[0].message

    def test_does_not_warn_on_a_complete_result(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            _collect([ROW] * 3, ROW_BYTES * 3)
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


class TestOversizedSingleRow:
    """The budget is a hard ceiling: a row that would overrun it is dropped."""

    def test_oversized_first_row_is_not_returned(self) -> None:
        """An operator capping the payload at N bytes must get at most N bytes.

        Returning the row anyway was the earlier behaviour, on the grounds
        that an empty list reads like "no matches". The flag carries that
        distinction instead — see ``truncation_message`` — so the limit can
        stay absolute.
        """
        bounded = _collect([{"blob": "x" * 50_000}], 1024)
        assert bounded.rows == []
        assert bounded.truncated is True
        assert bounded.bytes_returned <= 1024

    def test_oversized_row_followed_by_more(self) -> None:
        bounded = _collect([{"blob": "x" * 50_000}, ROW], 1024)
        assert bounded.row_count == 0
        assert bounded.truncated is True

    def test_rows_before_an_oversized_one_are_kept(self) -> None:
        """Only the overrunning row is dropped, not what preceded it."""
        bounded = _collect([ROW, {"blob": "x" * 50_000}], ROW_BYTES + 1024)
        assert bounded.rows == [ROW]
        assert bounded.truncated is True

    def test_empty_truncated_is_distinguishable_from_empty_complete(self) -> None:
        """Zero rows means two opposite things; only ``truncated`` separates them.

        A caller that reads emptiness as "no matches" would report the wrong
        answer for a collection whose rows are merely too wide.
        """
        too_wide = _collect([{"blob": "x" * 50_000}], 1024)
        no_matches = _collect([], 1024)

        assert too_wide.row_count == no_matches.row_count == 0
        assert too_wide.truncated is True
        assert no_matches.truncated is False

    def test_zero_row_message_does_not_read_as_an_empty_result(self) -> None:
        """ "Returned the first 0 row(s)" would describe the wrong failure."""
        message = _collect([{"blob": "x" * 50_000}], 1024).truncation_message()
        assert "not an empty result" in message
        assert "too wide" in message


class TestLaziness:
    def test_stops_pulling_from_the_source(self) -> None:
        """The feature is "stop reading", not "read everything then slice"."""
        pulled = 0

        def source():
            nonlocal pulled
            for _ in range(10_000):
                pulled += 1
                yield ROW

        bounded = _collect(source(), ROW_BYTES * 5)
        assert bounded.row_count == 5
        # 5 kept + at most one probe row to detect that more remained.
        assert pulled <= 6

    def test_reads_everything_when_it_fits(self) -> None:
        pulled = 0

        def source():
            nonlocal pulled
            for _ in range(3):
                pulled += 1
                yield ROW

        bounded = _collect(source(), 10_000)
        assert bounded.row_count == 3
        assert pulled == 3


class TestRowMeasurement:
    def test_non_json_values_do_not_raise(self) -> None:
        """Rows carry datetimes and Decimals; measuring must not fail the query."""
        rows = [{"at": datetime(2026, 1, 1), "amount": Decimal("1.5")}]
        bounded = _collect(rows, 10_000)
        assert bounded.rows == rows
        assert bounded.truncated is False

    def test_unserializable_row_still_terminates(self) -> None:
        """An unmeasurable row is charged a nominal size, never zero.

        Charging zero would let a stream of such rows loop until the source
        ran dry, which is exactly the unbounded read this module removes.
        """

        class Unserializable:
            def __repr__(self) -> str:
                raise RuntimeError("cannot repr")

        bounded = _collect([{"x": Unserializable()}] * 5, 2)
        assert bounded.row_count <= 5


class TestClamping:
    def test_above_maximum_is_clamped(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            assert (
                clamp_max_query_result_size(5 * 1024 * 1024)
                == MAX_MAX_QUERY_RESULT_SIZE
            )
        assert "exceeds the maximum" in caplog.text

    def test_below_minimum_is_clamped(self, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            assert clamp_max_query_result_size(10) == MIN_MAX_QUERY_RESULT_SIZE
        assert "below the minimum" in caplog.text

    @pytest.mark.parametrize(
        "value",
        [
            MIN_MAX_QUERY_RESULT_SIZE,
            DEFAULT_MAX_QUERY_RESULT_SIZE,
            MAX_MAX_QUERY_RESULT_SIZE,
        ],
    )
    def test_in_range_values_pass_through(self, value: int, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            assert clamp_max_query_result_size(value) == value
        assert caplog.text == ""


class TestSettingsLookup:
    def test_missing_key_uses_the_default(self) -> None:
        assert max_query_result_size_from({}) == DEFAULT_MAX_QUERY_RESULT_SIZE

    def test_none_uses_the_default(self) -> None:
        assert (
            max_query_result_size_from({"max_query_result_size": None})
            == DEFAULT_MAX_QUERY_RESULT_SIZE
        )

    def test_configured_value_is_honoured(self) -> None:
        assert max_query_result_size_from({"max_query_result_size": 20_480}) == 20_480

    def test_out_of_range_value_is_clamped_on_read(self) -> None:
        """An embedding host never passes through the CLI validator."""
        assert (
            max_query_result_size_from({"max_query_result_size": 99_999_999})
            == MAX_MAX_QUERY_RESULT_SIZE
        )

    def test_non_numeric_value_falls_back(self) -> None:
        assert (
            max_query_result_size_from({"max_query_result_size": "abc"})
            == DEFAULT_MAX_QUERY_RESULT_SIZE
        )

    def test_non_mapping_settings_fall_back(self) -> None:
        assert max_query_result_size_from(None) == DEFAULT_MAX_QUERY_RESULT_SIZE


class TestContextLookup:
    """A limit lookup must never be what fails an otherwise valid query."""

    def test_reads_the_configured_budget(self) -> None:
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context=SimpleNamespace(
                    settings={"max_query_result_size": 20_480}
                )
            )
        )
        assert max_query_result_size_for(ctx) == 20_480

    def test_lifespan_context_without_settings(self) -> None:
        """An embedding host may supply a context type that predates the key."""
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context=SimpleNamespace())
        )
        assert max_query_result_size_for(ctx) == DEFAULT_MAX_QUERY_RESULT_SIZE

    def test_context_without_request_context(self) -> None:
        assert (
            max_query_result_size_for(SimpleNamespace())
            == DEFAULT_MAX_QUERY_RESULT_SIZE
        )
