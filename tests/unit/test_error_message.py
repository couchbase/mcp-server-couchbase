"""SDK exceptions must reach the client as a short message, never a driver dump.

``str()`` of a Couchbase SDK exception embeds the statement, the bound
parameters (the whole embedding for vector tools) and node addresses. See
DA-2333 / DA-2337.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from couchbase.exceptions import (
    AuthenticationException,
    InternalServerFailureException,
    QueryErrorContext,
    UnAmbiguousTimeoutException,
)
from fastmcp.exceptions import ToolError

from cb_mcp.tools.operational.query import run_cluster_query, run_sql_plus_plus_query
from cb_mcp.utils.error_handling import wrap_with_error_handling

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


class _FakeSdkError(Exception):
    """Stands in for an SDK-core exception: it carries a context, a code and a message."""

    def __init__(self, ec, message, context):
        super().__init__(f"{type(self).__name__}(<ec={ec}, a very long dump {VECTOR}>)")
        self.error_code = ec
        self.message = message
        self.error_context = context


class DocumentNotFoundException(_FakeSdkError):  # noqa: N818 - mirrors the SDK class name
    pass


class QueryIndexNotFoundException(_FakeSdkError):  # noqa: N818 - mirrors the SDK class name
    pass


async def _client_message(error: Exception) -> str:
    """The message a client receives when a registered tool raises ``error``."""

    def tool() -> None:
        raise error

    with pytest.raises(ToolError) as exc_info:
        await wrap_with_error_handling(tool)()
    return str(exc_info.value)


class TestClientMessage:
    @pytest.mark.asyncio
    async def test_sdk_error_reduced_to_class_server_code_and_message(self) -> None:
        assert await _client_message(_query_error()) == (
            f"InternalServerFailureException 5430: {SERVER_MSG}"
        )

    @pytest.mark.asyncio
    async def test_class_distinguishes_errors_that_share_a_code_and_message(
        self,
    ) -> None:
        same = {"code": 5430, "msg": SERVER_MSG}
        assert await _client_message(
            _query_error(AuthenticationException, **same)
        ) != await _client_message(_query_error(InternalServerFailureException, **same))

    @pytest.mark.asyncio
    async def test_auth_error_keeps_role_hint(self) -> None:
        msg = "User does not have credentials. Add role query_system_catalog"
        result = await _client_message(
            _query_error(AuthenticationException, 13014, msg)
        )
        assert result == f"AuthenticationException 13014: {msg}"

    @pytest.mark.asyncio
    async def test_dump_details_never_leak(self) -> None:
        result = await _client_message(_query_error())
        for leaked in ("0.0123456789", "$query_vector", "10.0.0.1", "SELECT"):
            assert leaked not in result
        assert len(result) < 300

    @pytest.mark.asyncio
    async def test_sdk_error_with_only_a_message_is_class_and_message(self) -> None:
        err = UnAmbiguousTimeoutException(message="timed out")
        assert await _client_message(err) == "UnAmbiguousTimeoutException: timed out"

    @pytest.mark.asyncio
    async def test_plain_exceptions_unchanged(self) -> None:
        assert await _client_message(ValueError("bad input")) == "bad input"


class TestQueryHelpersPropagateSdkError:
    """Query helpers let the raw SDK exception through; the registration wrapper bounds it."""

    def test_run_cluster_query_propagates_sdk_error(self) -> None:
        cluster = MagicMock()
        cluster.query.side_effect = _query_error()
        with (
            patch(
                "cb_mcp.tools.operational.query.get_cluster_connection",
                return_value=cluster,
            ),
            pytest.raises(InternalServerFailureException) as exc_info,
        ):
            run_cluster_query(MagicMock(), "SELECT 1")
        assert SERVER_MSG in str(exc_info.value)

    def test_run_sql_plus_plus_query_propagates_sdk_error(self) -> None:
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
            pytest.raises(InternalServerFailureException) as exc_info,
        ):
            run_sql_plus_plus_query(MagicMock(), "b", "s", "SELECT 1")
        assert SERVER_MSG in str(exc_info.value)


class TestWrappedToolReportsShortError:
    """The registered tool is what a client calls: it must never carry the driver dump."""

    @pytest.mark.asyncio
    async def test_oversized_server_message_is_truncated(self) -> None:
        def tool() -> None:
            raise _query_error(msg="y" * 5_000)

        with pytest.raises(ToolError) as exc_info:
            await wrap_with_error_handling(tool)()
        assert len(str(exc_info.value)) == 500
        assert str(exc_info.value).startswith("InternalServerFailureException 5430: ")

    @pytest.mark.asyncio
    async def test_sdk_query_error_reaches_the_client_as_code_and_message(self) -> None:
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
            pytest.raises(ToolError) as exc_info,
        ):
            await wrap_with_error_handling(run_sql_plus_plus_query)(
                MagicMock(), "b", "s", "SELECT 1"
            )

        assert str(exc_info.value) == (
            f"InternalServerFailureException 5430: {SERVER_MSG}"
        )
        assert isinstance(exc_info.value.__cause__, InternalServerFailureException)
        for leaked in ("0.0123456789", "$query_vector", "10.0.0.1"):
            assert leaked not in str(exc_info.value)


class TestShortMessageForOtherSdkErrors:
    @pytest.mark.asyncio
    async def test_kv_error_is_class_code_and_message(self) -> None:
        err = DocumentNotFoundException(
            101, "Operation failed", SimpleNamespace(key="k", bucket_name="b")
        )
        assert await _client_message(err) == (
            "DocumentNotFoundException (ec=101): Operation failed"
        )

    @pytest.mark.asyncio
    async def test_search_error_uses_the_reason_from_the_response_body(self) -> None:
        body = json.dumps(
            {
                "error": "rest_auth: preparePerms, err: index not found",
                "request": {"query": {"vector": VECTOR}},
                "status": "fail",
            }
        )
        err = QueryIndexNotFoundException(
            17, "Streaming operation failed", SimpleNamespace(response_body=body)
        )
        result = await _client_message(err)
        assert result == (
            "QueryIndexNotFoundException (ec=17): "
            "rest_auth: preparePerms, err: index not found"
        )
        assert "0.0123456789" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", ["not json", "[]", '{"status": "fail"}', None])
    async def test_unusable_response_body_falls_back_to_the_message(self, body) -> None:
        err = QueryIndexNotFoundException(
            17, "Streaming operation failed", SimpleNamespace(response_body=body)
        )
        assert await _client_message(err) == (
            "QueryIndexNotFoundException (ec=17): Streaming operation failed"
        )

    @pytest.mark.asyncio
    async def test_a_context_with_no_code_or_message_falls_back_to_str(self) -> None:
        err = DocumentNotFoundException(None, "", SimpleNamespace())
        result = await _client_message(err)
        assert result.startswith("DocumentNotFoundException(<ec=None")
        assert len(result) == 500

    @pytest.mark.asyncio
    async def test_an_exception_without_a_context_is_never_reshaped(self) -> None:
        assert await _client_message(RuntimeError("Failed to reach any host")) == (
            "Failed to reach any host"
        )
