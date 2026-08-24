"""Thin client for Couchbase Enterprise Analytics' Server Async Request REST API.

This is the stateless core of the server. Every operation is a single,
self-contained HTTP call to EA identified only by *strings* (a request id or a
result-handle URL) — there is no live handle object and nothing is stored
server-side between calls. That is precisely what makes the MCP tools built on
top of this safe across multiple replicas and across restarts: the query state
lives on the EA server (keyed by request id), and any process holding the
string can act on it.

Verified endpoint contract (EA 2.2, Docker):

    start   POST   /api/v1/request            {statement, mode:"async"}
              -> {requestID, handle: <status URL>, status}
    status  GET    <status URL>
              -> {status, handle: <result URL>, resultCount, partitions, ...}
    fetch   GET    <result URL>
              -> {results: [...], metrics: {...}}
    discard DELETE <result URL>               -> 202
    cancel  DELETE /api/v1/active_requests?request_id=<requestID>  -> 200
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .constants import (
    ACTIVE_REQUESTS_PATH,
    MCP_SERVER_NAME,
    REQUEST_PATH,
    STATUS_READY,
    TERMINAL_FAILURE_STATUSES,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.ea_rest_client")


class EARestError(RuntimeError):
    """Raised when EA returns an error or an unexpected HTTP status."""


class EARestClient:
    """Stateless HTTP client for EA's async request API.

    A single ``httpx.Client`` (connection pool) is reused for efficiency, but it
    holds no per-query state — only TCP connections. All query identity travels
    in the request id / handle strings passed to each method.
    """

    def __init__(
        self,
        endpoint: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
        verify: bool = True,
    ) -> None:
        if not endpoint or not username or not password:
            raise ValueError(
                "EA endpoint, username, and password are all required "
                "(--endpoint/--username/--password or EA_ENDPOINT/EA_USERNAME/"
                "EA_PASSWORD)."
            )
        self._base = endpoint.rstrip("/")
        self._client = httpx.Client(
            auth=(username, password),
            timeout=timeout,
            verify=verify,
        )

    def close(self) -> None:
        self._client.close()

    # -- internal helpers ---------------------------------------------------

    def _url(self, path_or_handle: str) -> str:
        """Resolve a path or an EA-returned handle (already a path) to a URL."""
        if path_or_handle.startswith("http://") or path_or_handle.startswith("https://"):
            return path_or_handle
        return f"{self._base}{path_or_handle}"

    def _check(self, resp: httpx.Response, action: str) -> None:
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise EARestError(
                f"EA {action} failed: HTTP {resp.status_code} — {body}"
            )

    # -- the five operations ------------------------------------------------

    def start_query(self, statement: str) -> dict[str, Any]:
        """POST an async query. Returns {request_id, status_handle, status}."""
        resp = self._client.post(
            self._url(REQUEST_PATH),
            json={"statement": statement, "mode": "async"},
        )
        self._check(resp, "start_query")
        data = resp.json()
        request_id = data.get("requestID")
        status_handle = data.get("handle")
        if not request_id or not status_handle:
            raise EARestError(
                f"EA start_query response missing requestID/handle: {data}"
            )
        return {
            "request_id": request_id,
            "status_handle": status_handle,
            "status": data.get("status"),
        }

    def fetch_status(self, status_handle: str) -> dict[str, Any]:
        """GET the status handle. Returns readiness + result handle when ready.

        Returns a dict with:
            status: raw EA status string
            ready: True when results are ready to fetch
            failed: True on a terminal failure (cancelled/failed/timeout)
            result_handle: the result URL to fetch/discard (only when ready)
            result_count, errors, metrics: passthrough when present
        """
        resp = self._client.get(self._url(status_handle))
        self._check(resp, "fetch_status")
        data = resp.json()
        status = data.get("status")
        ready = status == STATUS_READY and data.get("handle") is not None
        failed = status in TERMINAL_FAILURE_STATUSES
        return {
            "status": status,
            "ready": ready,
            "failed": failed,
            "result_handle": data.get("handle"),
            "result_count": data.get("resultCount"),
            "errors": data.get("errors"),
            "metrics": data.get("metrics"),
        }

    def fetch_results(self, result_handle: str) -> dict[str, Any]:
        """GET the result handle. Returns {rows, metrics}."""
        resp = self._client.get(self._url(result_handle))
        self._check(resp, "fetch_results")
        data = resp.json()
        return {
            "rows": data.get("results", []),
            "metrics": data.get("metrics"),
        }

    def discard_results(self, result_handle: str) -> None:
        """DELETE the result handle to release server-side buffers."""
        resp = self._client.delete(self._url(result_handle))
        self._check(resp, "discard_results")

    def cancel_query(self, request_id: str) -> None:
        """DELETE the active request by id to cancel a running query."""
        resp = self._client.delete(
            self._url(ACTIVE_REQUESTS_PATH), params={"request_id": request_id}
        )
        self._check(resp, "cancel_query")

    def list_active_requests(self) -> list[dict[str, Any]]:
        """GET the currently-running requests from EA.

        EA is the source of truth here (this server holds no state), so this
        lets a caller recover a request id it has lost. Returns EA's active
        request records; each carries ``uuid`` (== the request id) plus state,
        statement, elapsed time, etc.
        """
        resp = self._client.get(self._url(ACTIVE_REQUESTS_PATH))
        self._check(resp, "list_active_requests")
        data = resp.json()
        return data if isinstance(data, list) else []
