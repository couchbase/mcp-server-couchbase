"""Tests for exporting query results with the ``copy_to_*`` arguments.

What these pin down, in order of what it would cost to get wrong:

1. A partial destination is rejected, never silently downgraded to a plain
   query. Running the query and returning rows when the caller asked for an
   export writes nothing and looks like success — the worst failure here.
2. The generated statement is the one the EA server actually accepts, with
   each of its three different quoting rules applied to the right argument.
3. An export is gated by read-only mode exactly like a hand-written
   ``COPY ... TO``, since it is one.
"""

from unittest.mock import patch

import pytest
from _oi_fakes import make_oi_ctx

from cb_mcp.tools.operational_insights.query import (
    CopyToError,
    build_copy_to_statement,
    oi_get_async_query_results,
    oi_run_query_async,
    oi_run_query_sync,
)

STATEMENT = "SELECT a.name, a.country FROM `travel-sample`.inventory.airline a LIMIT 10"
DESTINATION = {
    "copy_to_link": "s3TestLink",
    "copy_to_bucket": "sanjana-large-result-test",
    "copy_to_path": "copyto-test/run1",
}


class TestBuildCopyToStatement:
    def test_matches_the_shape_the_server_accepts(self) -> None:
        """The reference statement, verified by hand against a live EA cluster."""
        assert build_copy_to_statement(
            STATEMENT,
            link="s3TestLink",
            bucket="sanjana-large-result-test",
            path="copyto-test/run1",
        ) == (
            "COPY (\n"
            f"{STATEMENT}\n"
            ") AS t\n"
            "TO `sanjana-large-result-test` AT s3TestLink\n"
            'PATH("copyto-test/run1")\n'
            'WITH {"format": "json"}'
        )

    def test_bucket_is_backtick_quoted_and_escaped(self) -> None:
        """A bucket name is an identifier; an embedded backtick must be doubled."""
        built = build_copy_to_statement(STATEMENT, link="l", bucket="we`ird", path="p")
        assert "TO `we``ird` AT l" in built

    def test_path_is_a_quoted_literal(self) -> None:
        """A path is a string literal, so quotes in it must be escaped."""
        built = build_copy_to_statement(STATEMENT, link="l", bucket="b", path='p"q')
        assert 'PATH("p\\"q")' in built

    def test_trailing_semicolon_is_stripped(self) -> None:
        """The inner statement becomes a subquery; a ';' there is a syntax error."""
        built = build_copy_to_statement("SELECT 1;", link="l", bucket="b", path="p")
        assert "SELECT 1\n) AS t" in built

    @pytest.mark.parametrize("fmt", ["json", "parquet"])
    def test_supported_formats(self, fmt: str) -> None:
        built = build_copy_to_statement(
            STATEMENT, link="l", bucket="b", path="p", output_format=fmt
        )
        assert f'WITH {{"format": "{fmt}"}}' in built

    def test_format_is_case_insensitive(self) -> None:
        built = build_copy_to_statement(
            STATEMENT, link="l", bucket="b", path="p", output_format="PARQUET"
        )
        assert '"parquet"' in built

    @pytest.mark.parametrize("fmt", ["xml", "avro", "tsv"])
    def test_unsupported_format_is_rejected(self, fmt: str) -> None:
        """``tsv`` is here on purpose: the server rejects it outright."""
        with pytest.raises(CopyToError, match="not supported"):
            build_copy_to_statement(
                STATEMENT, link="l", bucket="b", path="p", output_format=fmt
            )

    def test_csv_is_rejected_despite_being_server_supported(self) -> None:
        """CSV needs a TYPE(...) clause these tools cannot infer.

        The server lists csv as a writing format but then fails compilation
        with "TYPE/AS Expression is required for csv format", so offering it
        would error on every call.
        """
        with pytest.raises(CopyToError, match="not supported"):
            build_copy_to_statement(
                STATEMENT, link="l", bucket="b", path="p", output_format="csv"
            )

    @pytest.mark.parametrize(
        "link",
        [
            "l; DROP DATASET x",  # statement break
            "l`x",  # backtick
            "l x",  # whitespace
            "1link",  # leading digit
            "",  # empty
        ],
    )
    def test_invalid_link_names_are_rejected(self, link: str) -> None:
        """The link is interpolated unquoted after AT, so it cannot be escaped.

        That makes the character class the only thing standing between a link
        name and SQL++ injection.
        """
        with pytest.raises(CopyToError):
            build_copy_to_statement(STATEMENT, link=link, bucket="b", path="p")

    @pytest.mark.parametrize("link", ["s3TestLink", "my_link", "my-link", "_l"])
    def test_valid_link_names_are_accepted(self, link: str) -> None:
        assert f"AT {link}" in build_copy_to_statement(
            STATEMENT, link=link, bucket="b", path="p"
        )


class TestRunQuerySyncExport:
    def _run(self, ctx, cluster, **kwargs):
        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            return oi_run_query_sync(ctx, STATEMENT, **kwargs)

    def test_export_returns_confirmation_and_no_rows(self) -> None:
        """The point of exporting is to keep the result out of the context."""
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        cluster.execute_query.return_value.get_all_rows.return_value = []

        result = self._run(ctx, cluster, **DESTINATION)

        assert result["success"] is True
        assert result["exported"] is True
        assert "rows" not in result
        assert result["destination"] == {
            "link": "s3TestLink",
            "bucket": "sanjana-large-result-test",
            "path": "copyto-test/run1",
            "format": "json",
        }

    def test_export_sends_the_wrapped_statement(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        cluster.execute_query.return_value.get_all_rows.return_value = []

        self._run(ctx, cluster, **DESTINATION)

        sent = cluster.execute_query.call_args[0][0]
        assert sent.startswith("COPY (")
        assert STATEMENT in sent
        assert "TO `sanjana-large-result-test` AT s3TestLink" in sent

    def test_without_copy_args_the_query_is_unchanged(self) -> None:
        """The arguments are optional: omitting them must not alter behaviour."""
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        cluster.execute_query.return_value.rows.side_effect = lambda: iter([{"a": 1}])

        result = self._run(ctx, cluster)

        assert cluster.execute_query.call_args[0][0] == STATEMENT
        assert result["rows"] == [{"a": 1}]
        assert "exported" not in result

    @pytest.mark.parametrize(
        "partial",
        [
            {"copy_to_link": "l"},
            {"copy_to_bucket": "b"},
            {"copy_to_path": "p"},
            {"copy_to_link": "l", "copy_to_bucket": "b"},
            {"copy_to_bucket": "b", "copy_to_path": "p"},
        ],
    )
    def test_partial_destination_is_rejected(self, partial: dict) -> None:
        """Never silently fall back to a plain query.

        That would return rows the caller did not ask for, write nothing, and
        give no sign the export never happened.
        """
        ctx, cluster = make_oi_ctx(read_only_mode=False)

        result = self._run(ctx, cluster, **partial)

        assert result["success"] is False
        assert "required to export" in result["error"]
        cluster.execute_query.assert_not_called()

    def test_format_without_destination_is_rejected(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=False)

        result = self._run(ctx, cluster, copy_to_format="parquet")

        assert result["success"] is False
        assert "without a destination" in result["error"]
        cluster.execute_query.assert_not_called()

    def test_invalid_link_is_an_error_envelope_not_a_raise(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=False)

        result = self._run(
            ctx,
            cluster,
            copy_to_link="bad; DROP DATASET x",
            copy_to_bucket="b",
            copy_to_path="p",
        )

        assert result["success"] is False
        assert "not a valid link name" in result["error"]
        cluster.execute_query.assert_not_called()


class TestExportUnderReadOnlyMode:
    """An export writes to external storage, so read-only mode must block it."""

    def test_blocked_when_read_only(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=True)

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            result = oi_run_query_sync(ctx, STATEMENT, **DESTINATION)

        assert result["success"] is False
        assert "blocked under read-only mode" in result["error"]
        cluster.execute_query.assert_not_called()

    def test_async_blocked_when_read_only(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=True)

        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            result = oi_run_query_async(ctx, STATEMENT, **DESTINATION)

        assert result["success"] is False
        assert "blocked under read-only mode" in result["error"]
        cluster.start_query.assert_not_called()


class TestRunQueryAsyncExport:
    def _run(self, ctx, **kwargs):
        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            return oi_run_query_async(ctx, STATEMENT, **kwargs)

    def test_export_returns_handle_and_destination(self) -> None:
        ctx, _cluster = make_oi_ctx(read_only_mode=False)

        result = self._run(ctx, **DESTINATION)

        assert result["success"] is True
        assert result["exported"] is True
        assert result["query_handle"]
        assert result["destination"]["bucket"] == "sanjana-large-result-test"
        assert "no rows" in result["message"]

    def test_export_starts_the_wrapped_statement(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=False)

        self._run(ctx, **DESTINATION)

        sent = cluster.start_query.call_args[0][0]
        assert sent.startswith("COPY (")
        assert STATEMENT in sent

    def test_without_copy_args_the_statement_is_unchanged(self) -> None:
        ctx, cluster = make_oi_ctx(read_only_mode=False)

        result = self._run(ctx)

        assert cluster.start_query.call_args[0][0] == STATEMENT
        assert "exported" not in result


class TestAsyncExportCompletion:
    """What the caller sees once an exporting async query finishes.

    A completed export returns zero rows, which on its own is
    indistinguishable from "the query matched nothing". These pin the fix:
    the destination travels on the registry entry so the completion response
    can name it.
    """

    def _finished_export(self):
        """Submit an export and force its handle to report results ready."""
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            submitted = oi_run_query_async(ctx, STATEMENT, **DESTINATION)

        handle = cluster.start_query.return_value
        status = handle.fetch_status.return_value
        status.results_ready.return_value = True
        result = status.result_handle.return_value.fetch_results.return_value
        result.rows.side_effect = lambda: iter([])
        result.get_all_rows.return_value = []
        return ctx, submitted["query_handle"]

    def test_completion_names_the_destination(self) -> None:
        ctx, token = self._finished_export()

        done = oi_get_async_query_results(ctx, token)

        assert done["ready"] is True
        assert done["exported"] is True
        assert done["destination"]["bucket"] == "sanjana-large-result-test"
        assert done["destination"]["path"] == "copyto-test/run1"

    def test_completion_message_says_the_export_finished(self) -> None:
        """Without this the caller cannot tell an export from an empty result."""
        ctx, token = self._finished_export()

        done = oi_get_async_query_results(ctx, token)

        assert "Export complete" in done["message"]
        assert "copyto-test/run1" in done["message"]

    def test_completion_returns_no_rows(self) -> None:
        ctx, token = self._finished_export()

        done = oi_get_async_query_results(ctx, token)

        assert done["row_count"] == 0
        assert "rows" not in done

    def test_non_export_completion_is_unchanged(self) -> None:
        """An ordinary async query must not grow export fields."""
        ctx, cluster = make_oi_ctx(read_only_mode=False)
        with patch(
            "cb_mcp.tools.operational_insights.query.get_access_token",
            return_value=None,
        ):
            submitted = oi_run_query_async(ctx, STATEMENT)

        status = cluster.start_query.return_value.fetch_status.return_value
        status.results_ready.return_value = True
        result = status.result_handle.return_value.fetch_results.return_value
        result.rows.side_effect = lambda: iter([{"a": 1}])

        done = oi_get_async_query_results(ctx, submitted["query_handle"])

        assert "exported" not in done
        assert "destination" not in done
        assert done["rows"] == [{"a": 1}]
