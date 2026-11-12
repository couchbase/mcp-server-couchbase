"""Unit tests for the REST-backed async query tools (``*_rest``).

The tools run against a real ``OperationalInsightsRestClient`` whose httpx
transport is a scripted fake of the Server Async Request API (``FakeEA``), so
the retry, error-mapping and streaming paths are exercised end to end without
a cluster. Each route serves a queue of responses; the last one repeats, so a
test scripts only the calls it cares about.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs

import httpx
import pytest

from cb_mcp.tools.operational_insights.query_rest import (
    operational_insights_cancel_async_query_rest,
    operational_insights_discard_async_query_results_rest,
    operational_insights_get_async_query_results_rest,
    operational_insights_run_query_async_rest,
)
from cb_mcp.utils.context import AppContext
from cb_mcp.utils.operational_insights.rest_client import (
    OperationalInsightsRestClient,
    OperationalInsightsRestError,
    QueryToken,
    UnknownQueryHandleError,
)
from cb_mcp.utils.query_limits import MAX_QUERY_RESULT_SIZE_KEY

RID = "5f0c-rid"
STATUS = f"/api/v1/request/status/{RID}/7-0"
RESULT = f"/api/v1/request/result/{RID}/7-0"
METRICS = {
    "elapsedTime": "875.709µs",
    "executionTime": "1.923459ms",
    "resultCount": 2,
    "resultSize": 16,
    "processedObjects": 0,
}

Factory = Callable[[httpx.Request], httpx.Response]


def j(status: int, body: object) -> Factory:
    return lambda request: httpx.Response(status, json=body)


def raw(status: int, content: bytes) -> Factory:
    return lambda request: httpx.Response(status, content=content)


def boom(exc: Exception) -> Factory:
    def factory(request: httpx.Request) -> httpx.Response:
        raise exc

    return factory


class FakeEA:
    """Scripted Server Async Request API, one response queue per route."""

    def __init__(self, **routes: list[Factory]) -> None:
        self.routes: dict[str, list[Factory]] = {
            "start": [j(200, {"requestID": RID, "handle": STATUS, "status": "queued"})],
            "status": [j(200, {"status": "success", "handle": RESULT})],
            "result": [j(200, {"results": [{"a": 1}, {"a": 2}], "metrics": METRICS})],
            "discard": [lambda r: httpx.Response(202)],
            "cancel": [lambda r: httpx.Response(200)],
        }
        self.routes.update(routes)
        self.requests: list[tuple[str, httpx.Request]] = []

    @staticmethod
    def _route(request: httpx.Request) -> str:
        path = request.url.path
        if request.method == "POST" and path == "/api/v1/request":
            return "start"
        if request.method == "GET" and path.startswith("/api/v1/request/status/"):
            return "status"
        if request.method == "GET" and path.startswith("/api/v1/request/result/"):
            return "result"
        if request.method == "DELETE" and path.startswith("/api/v1/request/result/"):
            return "discard"
        if request.method == "DELETE" and path == "/api/v1/active_requests":
            return "cancel"
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = self._route(request)
        self.requests.append((route, request))
        queue = self.routes[route]
        factory = queue.pop(0) if len(queue) > 1 else queue[0]
        return factory(request)

    def count(self, route: str) -> int:
        return sum(1 for r, _ in self.requests if r == route)

    def last(self, route: str) -> httpx.Request:
        return [req for r, req in self.requests if r == route][-1]


def make_ctx(
    fake: FakeEA, *, read_only_mode: bool = True, max_size: int | None = None
) -> SimpleNamespace:
    client = OperationalInsightsRestClient(
        "http://oi.test:8095",
        username="u",
        password="p",
        timeout_seconds=5,
        transport=httpx.MockTransport(fake),
    )
    client._sleep = lambda seconds: None
    provider = MagicMock()
    provider.get_rest_client.return_value = client
    settings = {} if max_size is None else {MAX_QUERY_RESULT_SIZE_KEY: max_size}
    app_context = AppContext(
        cluster_provider=provider,
        read_only_mode=read_only_mode,
        server_id="operational-insights",
        settings=settings,
    )
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=app_context)
    )


HANDLE = QueryToken(RID, STATUS).encode()


# ---------------------------------------------------------------------------
# The query_handle token
# ---------------------------------------------------------------------------


class TestQueryToken:
    def test_round_trips_with_destination(self) -> None:
        dest = {"link": "s3", "bucket": "b", "path": "p", "format": "json"}
        token = QueryToken(RID, STATUS, dest)
        assert QueryToken.decode(token.encode()) == token

    @pytest.mark.parametrize(
        "status_path",
        [
            "http://evil.example/api/v1/request/status/x",  # another host
            "/api/v1/request/result/x",  # wrong prefix
            "/api/v1/request/status/../../admin",  # path traversal
            "/api/v1/request/status/x?redirect=1",  # query string
        ],
    )
    def test_rejects_paths_that_could_redirect_a_request(
        self, status_path: str
    ) -> None:
        with pytest.raises(UnknownQueryHandleError):
            QueryToken.decode(QueryToken(RID, status_path).encode())

    @pytest.mark.parametrize("token", ["deadbeef" * 4, "", "not base64 !!", "e30"])
    def test_rejects_garbage_and_sdk_tokens(self, token: str) -> None:
        with pytest.raises(UnknownQueryHandleError):
            QueryToken.decode(token)


# ---------------------------------------------------------------------------
# operational_insights_run_query_async_rest
# ---------------------------------------------------------------------------


class TestRunQueryAsyncRest:
    def test_starts_query_and_returns_a_decodable_handle(self) -> None:
        fake = FakeEA()
        out = operational_insights_run_query_async_rest(make_ctx(fake), "SELECT 1")

        assert out["success"] is True
        assert QueryToken.decode(out["query_handle"]) == QueryToken(RID, STATUS)
        assert "operational_insights_get_async_query_results_rest" in out["message"]
        body = json.loads(fake.last("start").content)
        assert body["statement"] == "SELECT 1"
        assert body["mode"] == "async"
        assert body["timeout"] == "605000.0ms"

    def test_read_only_mode_sends_readonly_flag(self) -> None:
        fake = FakeEA()
        operational_insights_run_query_async_rest(
            make_ctx(fake, read_only_mode=True), "SELECT 1"
        )
        assert json.loads(fake.last("start").content)["readonly"] is True

    def test_write_mode_omits_readonly_flag(self) -> None:
        fake = FakeEA()
        operational_insights_run_query_async_rest(
            make_ctx(fake, read_only_mode=False), "SELECT 1"
        )
        assert "readonly" not in json.loads(fake.last("start").content)

    def test_copy_to_blocked_in_read_only_mode_without_calling_server(self) -> None:
        fake = FakeEA()
        out = operational_insights_run_query_async_rest(
            make_ctx(fake),
            "SELECT 1",
            copy_to_link="s3",
            copy_to_bucket="b",
            copy_to_path="p",
        )
        assert out["success"] is False
        assert "COPY ... TO is blocked" in out["error"]
        assert fake.requests == []

    def test_export_carries_destination_in_response_and_handle(self) -> None:
        fake = FakeEA()
        out = operational_insights_run_query_async_rest(
            make_ctx(fake, read_only_mode=False),
            "SELECT 1",
            copy_to_link="s3",
            copy_to_bucket="b",
            copy_to_path="p",
        )
        dest = {"link": "s3", "bucket": "b", "path": "p", "format": "json"}
        assert out["exported"] is True and out["destination"] == dest
        assert QueryToken.decode(out["query_handle"]).destination == dest
        assert json.loads(fake.last("start").content)["statement"].startswith("COPY (")

    def test_retries_503_then_succeeds(self) -> None:
        fake = FakeEA(start=[j(503, {}), j(503, {}), *FakeEA().routes["start"]])
        out = operational_insights_run_query_async_rest(make_ctx(fake), "SELECT 1")
        assert out["success"] is True
        assert fake.count("start") == 3

    def test_retries_errors_the_server_marks_retriable(self) -> None:
        retriable = j(
            200, {"errors": [{"code": 23007, "msg": "busy", "retriable": True}]}
        )
        fake = FakeEA(start=[retriable, *FakeEA().routes["start"]])
        out = operational_insights_run_query_async_rest(make_ctx(fake), "SELECT 1")
        assert out["success"] is True
        assert fake.count("start") == 2

    def test_retries_connect_failures(self) -> None:
        fake = FakeEA(
            start=[boom(httpx.ConnectError("refused")), *FakeEA().routes["start"]]
        )
        out = operational_insights_run_query_async_rest(make_ctx(fake), "SELECT 1")
        assert out["success"] is True
        assert fake.count("start") == 2

    def test_does_not_retry_non_retriable_errors(self) -> None:
        fatal = j(
            400,
            {
                "errors": [
                    {
                        "code": 24045,
                        "msg": "Cannot find dataset nope",
                        "retriable": False,
                    }
                ]
            },
        )
        fake = FakeEA(start=[fatal])
        out = operational_insights_run_query_async_rest(
            make_ctx(fake), "SELECT * FROM nope"
        )
        assert out == {
            "success": False,
            "error": "24045: Cannot find dataset nope",
            "statement": "SELECT * FROM nope",
        }
        assert fake.count("start") == 1

    def test_prefers_the_non_retriable_error_when_mixed(self) -> None:
        mixed = j(
            400,
            {
                "errors": [
                    {"code": 1, "msg": "transient", "retriable": True},
                    {"code": 2, "msg": "permanent", "retriable": False},
                ]
            },
        )
        out = operational_insights_run_query_async_rest(
            make_ctx(FakeEA(start=[mixed])), "SELECT 1"
        )
        assert out["error"] == "2: permanent"

    def test_unknown_error_code_fails_cleanly(self) -> None:
        odd = j(500, {"errors": [{"code": 99999, "msg": "something new"}]})
        out = operational_insights_run_query_async_rest(
            make_ctx(FakeEA(start=[odd])), "SELECT 1"
        )
        assert out["error"] == "99999: something new"

    def test_gives_up_after_max_retries(self) -> None:
        fake = FakeEA(
            start=[
                j(
                    503,
                    {
                        "errors": [
                            {
                                "code": 23000,
                                "msg": "Service unavailable",
                                "retriable": True,
                            }
                        ]
                    },
                )
            ]
        )
        out = operational_insights_run_query_async_rest(make_ctx(fake), "SELECT 1")
        assert out["success"] is False
        assert out["error"] == "23000: Service unavailable (gave up after 4 attempts)"
        assert fake.count("start") == 4

    def test_never_retries_a_start_that_timed_out_after_sending(self) -> None:
        """The server may already be running it; a re-send could run it twice."""
        fake = FakeEA(
            start=[boom(httpx.ReadTimeout("slow")), *FakeEA().routes["start"]]
        )
        out = operational_insights_run_query_async_rest(make_ctx(fake), "SELECT 1")
        assert out["success"] is False
        assert "Timed out" in out["error"]
        assert fake.count("start") == 1

    def test_tls_errors_are_not_retried(self) -> None:
        fake = FakeEA(
            start=[boom(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"))]
        )
        out = operational_insights_run_query_async_rest(make_ctx(fake), "SELECT 1")
        assert "TLS connection error" in out["error"]
        assert fake.count("start") == 1


# ---------------------------------------------------------------------------
# operational_insights_get_async_query_results_rest
# ---------------------------------------------------------------------------


class TestGetAsyncQueryResultsRest:
    def test_not_ready(self) -> None:
        fake = FakeEA(status=[j(200, {"status": "running"})])
        out = operational_insights_get_async_query_results_rest(make_ctx(fake), HANDLE)
        assert out == {
            "success": True,
            "query_handle": HANDLE,
            "ready": False,
            "message": "Query is still running. Call this tool again later to check for results.",
        }
        assert fake.count("result") == 0

    def test_ready_returns_rows_and_sdk_shaped_metadata(self) -> None:
        out = operational_insights_get_async_query_results_rest(
            make_ctx(FakeEA()), HANDLE
        )
        assert out["rows"] == [{"a": 1}, {"a": 2}]
        assert out["row_count"] == 2
        assert out["truncated"] is False
        # Same values the SDK tools report: durations round to whole
        # microseconds on the way through timedelta.
        assert out["metadata"] == {
            "warnings": [],
            "metrics": {
                "elapsed_time_ms": 0.876,
                "execution_time_ms": 1.923,
                "result_count": 2,
                "result_size": 16,
                "processed_objects": 0,
            },
        }

    def test_warnings_are_plain_dicts(self) -> None:
        body = {
            "results": [1],
            "warnings": [{"code": 1, "msg": "careful"}],
            "metrics": METRICS,
        }
        out = operational_insights_get_async_query_results_rest(
            make_ctx(FakeEA(result=[j(200, body)])), HANDLE
        )
        assert out["metadata"]["warnings"] == [{"code": 1, "message": "careful"}]

    def test_row_shapes_survive_streaming(self) -> None:
        """Scalars, floats, big ints, nested containers, and a key named ``item``
        (which shares ijson's array-item prefix) all come through intact."""
        rows = [
            42,
            1.5,
            12345678901234567,
            None,
            "s",
            [1, [2]],
            {"item": {"item": [1]}, "x": 2.25},
        ]
        out = operational_insights_get_async_query_results_rest(
            make_ctx(FakeEA(result=[j(200, {"results": rows, "metrics": METRICS})])),
            HANDLE,
        )
        assert out["rows"] == rows
        assert type(out["rows"][1]) is float

    def test_budget_truncates_and_drops_metadata(self) -> None:
        body = {"results": [{"i": n} for n in range(100)], "metrics": METRICS}
        out = operational_insights_get_async_query_results_rest(
            make_ctx(FakeEA(result=[j(200, body)]), max_size=50), HANDLE
        )
        assert out["truncated"] is True
        assert 0 < out["row_count"] < 100
        assert out["metadata"] == {}
        assert out["truncation"]["limit_bytes"] == 50

    def test_errors_after_rows_are_never_returned_as_success(self) -> None:
        body = {
            "results": [{"a": 1}, {"a": 2}],
            "errors": [{"code": 23001, "msg": "Out of memory while processing"}],
            "metrics": METRICS,
        }
        out = operational_insights_get_async_query_results_rest(
            make_ctx(FakeEA(result=[j(200, body)])), HANDLE
        )
        assert out == {
            "success": False,
            "error": "23001: Out of memory while processing",
            "query_handle": HANDLE,
        }

    def test_error_status_is_never_parsed_as_rows(self) -> None:
        out = operational_insights_get_async_query_results_rest(
            make_ctx(FakeEA(result=[j(500, {"status": "fatal"})])), HANDLE
        )
        assert out["success"] is False
        assert "HTTP 500" in out["error"]

    def test_retries_503_on_the_result_fetch(self) -> None:
        fake = FakeEA(result=[j(503, {}), *FakeEA().routes["result"]])
        out = operational_insights_get_async_query_results_rest(make_ctx(fake), HANDLE)
        assert out["rows"] == [{"a": 1}, {"a": 2}]
        assert fake.count("result") == 2

    def test_cut_off_body_is_refetched_once(self) -> None:
        cut = raw(200, b'{"results": [{"a": 1}, {"a": ')
        fake = FakeEA(result=[cut, *FakeEA().routes["result"]])
        out = operational_insights_get_async_query_results_rest(make_ctx(fake), HANDLE)
        assert out["success"] is True
        assert out["rows"] == [{"a": 1}, {"a": 2}]
        assert fake.count("result") == 2

    def test_connection_dropped_mid_body_is_refetched_once(self) -> None:
        def dropped(request: httpx.Request) -> httpx.Response:
            def chunks():
                yield b'{"results": [{"a": 1},'
                raise httpx.RemoteProtocolError("peer closed connection")

            return httpx.Response(200, content=chunks())

        fake = FakeEA(result=[dropped, *FakeEA().routes["result"]])
        out = operational_insights_get_async_query_results_rest(make_ctx(fake), HANDLE)
        assert out["rows"] == [{"a": 1}, {"a": 2}]

    def test_cut_off_twice_gives_a_clear_error(self) -> None:
        fake = FakeEA(result=[raw(200, b'{"results": [{"a": 1}, ')])
        out = operational_insights_get_async_query_results_rest(make_ctx(fake), HANDLE)
        assert out["success"] is False
        assert "result download was cut off" in out["error"]
        assert "results are still on the server" in out["error"]
        assert fake.count("result") == 2

    def test_failed_query_reports_the_server_error(self) -> None:
        failed = j(
            200,
            {
                "status": "failed",
                "errors": [{"code": 23010, "msg": "Job has been cancelled"}],
            },
        )
        out = operational_insights_get_async_query_results_rest(
            make_ctx(FakeEA(status=[failed])), HANDLE
        )
        assert out["error"] == "23010: Job has been cancelled"

    def test_failed_status_without_details_is_still_an_error(self) -> None:
        out = operational_insights_get_async_query_results_rest(
            make_ctx(FakeEA(status=[j(200, {"status": "timeout"})])), HANDLE
        )
        assert out == {
            "success": False,
            "error": "Query timeout.",
            "query_handle": HANDLE,
        }

    def test_unknown_handle_on_404(self) -> None:
        out = operational_insights_get_async_query_results_rest(
            make_ctx(FakeEA(status=[j(404, {})])), HANDLE
        )
        assert out["success"] is False
        assert out["error"].startswith(f"Unknown query_handle '{HANDLE}'.")

    def test_sdk_handle_is_rejected_without_a_request(self) -> None:
        fake = FakeEA()
        out = operational_insights_get_async_query_results_rest(
            make_ctx(fake), "ab" * 16
        )
        assert out["error"].startswith("Unknown query_handle")
        assert "without the _rest suffix" in out["error"]
        assert fake.requests == []

    def test_server_handles_are_reduced_to_their_path(self) -> None:
        """An absolute handle naming another host is not followed."""
        fake = FakeEA(
            status=[
                j(200, {"status": "success", "handle": f"http://10.0.0.9:8095{RESULT}"})
            ]
        )
        operational_insights_get_async_query_results_rest(make_ctx(fake), HANDLE)
        assert fake.last("result").url.host == "oi.test"

    def test_finished_export_names_the_destination(self) -> None:
        dest = {"link": "s3", "bucket": "b", "path": "p", "format": "parquet"}
        handle = QueryToken(RID, STATUS, dest).encode()
        fake = FakeEA(result=[j(200, {"results": [], "metrics": METRICS})])
        out = operational_insights_get_async_query_results_rest(make_ctx(fake), handle)
        assert out["exported"] is True
        assert out["row_count"] == 0
        assert out["destination"] == dest
        assert "b/p as parquet" in out["message"]


# ---------------------------------------------------------------------------
# discard / cancel
# ---------------------------------------------------------------------------


class TestDiscardAsyncQueryResultsRest:
    def test_not_ready_keeps_the_query(self) -> None:
        fake = FakeEA(status=[j(200, {"status": "running"})])
        out = operational_insights_discard_async_query_results_rest(
            make_ctx(fake), HANDLE
        )
        assert out["discarded"] is False and out["ready"] is False
        assert "operational_insights_cancel_async_query_rest" in out["message"]
        assert fake.count("discard") == 0

    def test_ready_deletes_the_result_handle(self) -> None:
        fake = FakeEA()
        out = operational_insights_discard_async_query_results_rest(
            make_ctx(fake), HANDLE
        )
        assert out == {"success": True, "query_handle": HANDLE, "discarded": True}
        assert fake.last("discard").url.path == RESULT

    def test_404_on_delete_counts_as_discarded(self) -> None:
        fake = FakeEA(discard=[lambda r: httpx.Response(404)])
        out = operational_insights_discard_async_query_results_rest(
            make_ctx(fake), HANDLE
        )
        assert out["discarded"] is True


class TestCancelAsyncQueryRest:
    def test_completed_query_is_not_cancelled(self) -> None:
        fake = FakeEA()
        out = operational_insights_cancel_async_query_rest(make_ctx(fake), HANDLE)
        assert out["cancelled"] is False
        assert "operational_insights_discard_async_query_results_rest" in out["message"]
        assert fake.count("cancel") == 0

    def test_running_query_is_cancelled_by_request_id(self) -> None:
        fake = FakeEA(status=[j(200, {"status": "running"})])
        out = operational_insights_cancel_async_query_rest(make_ctx(fake), HANDLE)
        assert out == {"success": True, "query_handle": HANDLE, "cancelled": True}
        assert parse_qs(fake.last("cancel").content.decode()) == {"request_id": [RID]}

    def test_cancel_failure_is_reported(self) -> None:
        fake = FakeEA(status=[j(200, {"status": "running"})], cancel=[j(500, {})])
        out = operational_insights_cancel_async_query_rest(make_ctx(fake), HANDLE)
        assert out["success"] is False and "HTTP 500" in out["error"]


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


class TestFromSettings:
    def test_refuses_client_certificate_auth_for_now(self) -> None:
        with pytest.raises(OperationalInsightsRestError, match="Client certificate"):
            OperationalInsightsRestClient.from_settings(
                {"connection_string": "https://oi:18095", "client_cert_path": "/c.pem"},
                timeout_seconds=5,
            )

    def test_requires_username_and_password(self) -> None:
        with pytest.raises(OperationalInsightsRestError, match="username, password"):
            OperationalInsightsRestClient.from_settings(
                {"connection_string": "http://oi:8095"}, timeout_seconds=5
            )

    def test_requires_an_http_url(self) -> None:
        with pytest.raises(OperationalInsightsRestError, match="http"):
            OperationalInsightsRestClient.from_settings(
                {
                    "connection_string": "couchbase://oi",
                    "username": "u",
                    "password": "p",
                },
                timeout_seconds=5,
            )
