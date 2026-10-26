"""SDK exceptions must reach the client as a short message, never a driver dump.

``str()`` of a Couchbase SDK exception embeds the statement, the bound
parameters (the whole embedding for vector tools) and node addresses. See
DA-2333 / DA-2337.
"""

from unittest.mock import MagicMock, patch

import pytest
from couchbase.exceptions import (
    AuthenticationException,
    InternalServerFailureException,
    QueryErrorContext,
    UnAmbiguousTimeoutException,
)

from cb_mcp.tools.operational.query import run_cluster_query, run_sql_plus_plus_query
from cb_mcp.utils.constants import MAX_ERROR_MESSAGE_LENGTH
from cb_mcp.utils.responses import error_message, tool_error

VECTOR = str([0.0123456789] * 1536)
SERVER_MSG = (
    "Invalid parameter (query vector) specified for vector search function: "
    "number of dimension (3072) does not match index vector dimension (1536)"
)


def _query_error(exc_cls=InternalServerFailureException, code=5430, msg=SERVER_MSG):
    context = QueryErrorContext(
        context_type="QueryErrorContext",
        first_error_code=code,
        first_error_message=msg,
        statement="SELECT APPROX_VECTOR_DISTANCE(embedding, $query_vector) ...",
        parameters='{"$query_vector":' + VECTOR + "}",
        last_dispatched_to="10.0.0.1:8093",
    )
    return exc_cls(message="Streaming operation failed", context=context)


class TestErrorMessage:
    def test_sdk_error_reduced_to_server_code_and_message(self) -> None:
        assert error_message(_query_error()) == f"5430: {SERVER_MSG}"

    def test_auth_error_keeps_role_hint(self) -> None:
        msg = "User does not have credentials. Add role query_system_catalog"
        result = error_message(_query_error(AuthenticationException, 13014, msg))
        assert result == f"13014: {msg}"

    def test_dump_details_never_leak(self) -> None:
        result = error_message(_query_error())
        for leaked in ("0.0123456789", "$query_vector", "10.0.0.1", "SELECT"):
            assert leaked not in result
        assert len(result) < 300

    def test_sdk_error_without_server_detail_drops_context(self) -> None:
        err = UnAmbiguousTimeoutException(message="timed out")
        result = error_message(err)
        assert result == "UnAmbiguousTimeoutException (see server log for details)"

    def test_plain_exceptions_and_strings_unchanged(self) -> None:
        assert error_message(ValueError("bad input")) == "bad input"
        assert error_message("plain text") == "plain text"

    def test_oversized_non_sdk_error_is_truncated(self) -> None:
        result = error_message(RuntimeError("x" * 50_000))
        assert result.startswith("x" * MAX_ERROR_MESSAGE_LENGTH)
        assert result.endswith("... [truncated]")
        assert len(result) < 600

    def test_oversized_server_message_is_truncated(self) -> None:
        result = error_message(_query_error(msg="y" * 5_000))
        assert len(result) < 600

    def test_tool_error_envelope_is_condensed(self) -> None:
        envelope = tool_error(_query_error(), index_name="idx")
        assert envelope == {
            "success": False,
            "error": f"5430: {SERVER_MSG}",
            "index_name": "idx",
        }


class TestQueryHelpersRaiseShortError:
    """Raising paths (query health tools) are stringified by the framework."""

    def test_run_cluster_query_raises_short_message(self) -> None:
        cluster = MagicMock()
        cluster.query.side_effect = _query_error()
        with (
            patch(
                "cb_mcp.tools.operational.query.get_cluster_connection",
                return_value=cluster,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            run_cluster_query(MagicMock(), "SELECT 1")
        assert str(exc_info.value) == f"5430: {SERVER_MSG}"
        assert isinstance(exc_info.value.__cause__, InternalServerFailureException)

    def test_run_sql_plus_plus_query_raises_short_message(self) -> None:
        scope = MagicMock()
        scope.query.side_effect = _query_error()
        bucket = MagicMock()
        bucket.scope.return_value = scope
        with (
            patch(
                "cb_mcp.tools.operational.query.get_cluster_connection",
                return_value=MagicMock(),
            ),
            patch(
                "cb_mcp.tools.operational.query.connect_to_bucket",
                return_value=bucket,
            ),
            pytest.raises(RuntimeError) as exc_info,
        ):
            run_sql_plus_plus_query(MagicMock(), "b", "s", "SELECT 1")
        assert str(exc_info.value) == f"5430: {SERVER_MSG}"
