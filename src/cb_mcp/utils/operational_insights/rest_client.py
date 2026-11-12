"""Stateless REST client for the Operational Insights Server Async Request API.

Backs the ``*_rest`` variants of the async query tools
(``tools/operational_insights/query_rest.py``). They exist alongside the
SDK-backed tools so the two can be compared before either is retired.

Why REST at all
---------------
The SDK's async flow hands back live ``BlockingQueryHandle`` objects, which is
why the SDK tools keep a server-side ``QueryResultsRegistry`` (see
``handle_registry.py``) and cannot run on more than one worker. But a handle is
really just two strings — the server's ``requestID`` and a status-handle path —
plus shared client plumbing. This module carries those strings *in the
``query_handle`` token itself* (see :class:`QueryToken`), so nothing is held
between calls: any process, any replica, any restart can serve any call.

What the SDK did that this module now does itself
-------------------------------------------------
* **Retries** (:meth:`OperationalInsightsRestClient._send`): the SDK's rules —
  connect failures, HTTP 503, and errors the server marks ``retriable`` — with
  tighter limits (see the ``REST_*`` constants). Nothing is retried once a
  request may have reached the server and started work (a read or write
  timeout), because re-sending a start could run the statement twice.
* **Error mapping** (:func:`_raise_for_error_body`): any HTTP error status or
  ``errors`` key becomes an :class:`OperationalInsightsRestError` carrying
  the server's own ``code: message``. A response is never parsed for rows
  unless its status is a success.
* **Streaming** (:meth:`OperationalInsightsRestClient.fetch_results`): the
  result body is parsed incrementally with ``ijson`` — the same parser the SDK
  uses internally — so ``collect_rows_within_budget`` can stop reading once the
  size budget is spent, exactly as it does on the SDK's row iterator.
* **Late errors**: an ``errors`` array that arrives *after* some rows fails the
  fetch rather than returning those rows as a successful partial result.
* **Broken streams**: a body cut off mid-row is re-fetched once from the start.
  That is safe here and not in the SDK: fetching does not consume the results
  on the server, and nothing has been returned to the caller yet.

Timeouts
--------
``operational_insights_async_query_result_timeout`` is applied as an httpx
timeout, which bounds each *wait* (connect, or the gap between two chunks of a
body) rather than the whole call. The SDK applies the same setting as a
deadline on the entire fetch, row stream included, which is why a large
result could not be fetched through it at all. Here a long download that
keeps producing data completes; a server that goes silent still fails.

Authentication
--------------
Username/password only, for now. A configured client certificate is refused
with a clear error rather than silently falling back to the password, which
would make these tools authenticate as a different identity from the SDK
tools.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import random
import re
import ssl
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import ijson

from ...servers.operational_insights.constants import (
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
    REST_BACKOFF_MAX_SECONDS,
    REST_BACKOFF_MIN_SECONDS,
    REST_MAX_RETRIES,
    REST_QUERY_TIMEOUT,
    REST_RETRY_BUDGET_SECONDS,
)
from ..query_limits import BoundedRows, collect_rows_within_budget

logger = logging.getLogger(f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.utils.rest_client")

#: Server Async Request API paths (same ones the SDK uses).
REQUEST_PATH = "/api/v1/request"
ACTIVE_REQUESTS_PATH = "/api/v1/active_requests"
STATUS_PATH_PREFIX = "/api/v1/request/status/"
RESULT_PATH_PREFIX = "/api/v1/request/result/"

#: Terminal statuses that mean the query will never produce results.
FAILED_STATUSES = frozenset({"failed", "fatal", "timeout", "cancelled"})


class OperationalInsightsRestError(Exception):
    """A failure whose message is already phrased for the MCP client."""


class UnknownQueryHandleError(OperationalInsightsRestError):
    """The token is malformed, or the server no longer knows the query.

    Carries no message of its own: the tools format one around the token the
    caller passed, matching the SDK tools' wording.
    """


class _StreamBrokenError(Exception):
    """The result body ended early or became unparseable; safe to re-fetch."""


# --------------------------------------------------------------------------
# The query_handle token
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryToken:
    """Everything a follow-up call needs, carried by the client as one string.

    ``destination`` rides along for the same reason it lives on the SDK
    tools' ``HandleEntry``: a finished ``COPY ... TO`` returns zero rows, and
    only the destination makes that distinguishable from an empty result.

    The token is a bearer capability — whoever holds it can fetch, discard or
    cancel that query with this server's credentials. It is not signed, so
    :meth:`decode` treats every field as untrusted: the status path must be a
    server-relative path under the status prefix, and it is always joined to
    the configured endpoint, never followed to another host.
    """

    request_id: str
    status_path: str
    destination: dict[str, Any] | None = None

    def encode(self) -> str:
        payload: dict[str, Any] = {"r": self.request_id, "s": self.status_path}
        if self.destination is not None:
            payload["d"] = self.destination
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> QueryToken:
        """Parse and validate a token, or raise ``UnknownQueryHandleError``."""
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            payload = json.loads(raw)
            request_id = payload["r"]
            status_path = payload["s"]
            destination = payload.get("d")
        except (binascii.Error, ValueError, KeyError, TypeError, AttributeError):
            raise UnknownQueryHandleError() from None
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(status_path, str)
            or not _is_server_path(status_path, STATUS_PATH_PREFIX)
            or not (destination is None or isinstance(destination, dict))
        ):
            raise UnknownQueryHandleError()
        return cls(request_id, status_path, destination)


def _is_server_path(path: str, prefix: str) -> bool:
    """True for a plain server-relative path under ``prefix``.

    Rejects anything that could redirect a request — a scheme or host, a
    query or fragment, or a ``..`` segment — so a tampered token can only
    ever address this API on the configured endpoint.
    """
    return (
        path.startswith(prefix)
        and "://" not in path
        and "?" not in path
        and "#" not in path
        and ".." not in path.split("/")
    )


def _server_path(handle: str, prefix: str) -> str:
    """Reduce a handle the server returned to its path, and validate it.

    The SDK does the same (``build_request_from_handle`` keeps only the
    path): a handle naming a node's internal address must not be followed.
    """
    path = urlsplit(handle).path if "://" in handle else handle
    if not _is_server_path(path, prefix):
        raise OperationalInsightsRestError(
            f"Server returned an unexpected handle: {handle!r}"
        )
    return path


# --------------------------------------------------------------------------
# Errors and metadata
# --------------------------------------------------------------------------


def _format_errors(errors: Any) -> str:
    """``code: message`` for the error the SDK would surface.

    Like the SDK's ``ErrorMapper``, prefers the first non-retriable error:
    it is the one that explains why the request failed for good.
    """
    if not isinstance(errors, list) or not errors:
        return "The server reported an error but sent no details."
    chosen = next(
        (e for e in errors if isinstance(e, dict) and not e.get("retriable")),
        errors[0],
    )
    if not isinstance(chosen, dict):
        return str(chosen)
    return f"{chosen.get('code')}: {chosen.get('msg')}"


def _all_retriable(errors: Any) -> bool:
    return (
        isinstance(errors, list)
        and bool(errors)
        and all(isinstance(e, dict) and e.get("retriable") for e in errors)
    )


def _response_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _raise_for_error_body(resp: httpx.Response, action: str) -> dict[str, Any]:
    """Return the JSON body of a successful response, or raise.

    Any ``errors`` key fails the call, whatever the status code — the
    server reports some failures with HTTP 200.
    """
    if resp.status_code == 401:
        raise OperationalInsightsRestError("Invalid credentials provided (HTTP 401).")
    body = _response_json(resp)
    if isinstance(body, dict) and body.get("errors"):
        raise OperationalInsightsRestError(_format_errors(body["errors"]))
    if resp.status_code >= 400:
        raise OperationalInsightsRestError(
            f"Operational Insights {action} failed with HTTP {resp.status_code}."
        )
    if not isinstance(body, dict):
        raise OperationalInsightsRestError(
            f"Operational Insights {action} returned a response that is not a JSON object."
        )
    return body


_DURATION_FULL = re.compile(r"(?:\d*(?:\.\d*)?(?:ns|us|µs|μs|ms|s|m|h))+")
_DURATION_PAIRS = re.compile(r"(\d*(?:\.\d*)?)(ns|us|ms|s|m|h)")
_UNIT_SECONDS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def _duration_ms(raw: Any) -> float:
    """Parse a server duration (``"875.709µs"``, ``"1m2.5s"``) to milliseconds.

    Mirrors the SDK's ``parse_duration_str``, including its fallback: an
    unparseable value is 0 rather than an error, since metadata is a nicety.
    """
    if raw in (None, "", "0"):
        return 0.0
    if not isinstance(raw, str) or not _DURATION_FULL.fullmatch(raw):
        return 0.0
    normalized = raw.replace("µs", "us").replace("μs", "us")
    total = 0.0
    for number, unit in _DURATION_PAIRS.findall(normalized):
        try:
            total += float(number) * _UNIT_SECONDS[unit]
        except (ValueError, KeyError):
            return 0.0
    return total * 1e3


def _sdk_ms(raw: Any) -> float:
    """Milliseconds exactly as the SDK tools report them.

    The SDK stores the duration as a ``timedelta`` (whole microseconds) and the
    tools read it back with ``total_seconds() * 1000``; going through the same
    two steps keeps the two variants' metrics identical, rounding included.
    """
    return timedelta(microseconds=_duration_ms(raw) * 1000).total_seconds() * 1000


def build_metadata(trailer: Mapping[str, Any], *, complete: bool) -> dict[str, Any]:
    """The ``metadata`` block, in the SDK tools' shape.

    ``complete`` is False when the row stream was cut short by the size
    budget: the server sends warnings and metrics *after* the rows, so a
    truncated read never saw them — the SDK tools return ``{}`` then too.

    Warnings are emitted as ``{"code", "message"}`` dicts. (The SDK tools pass
    the SDK's ``QueryWarning`` objects through as-is, which are not plain
    JSON; this is the shape those objects describe.)
    """
    if not complete:
        return {}
    warnings = [
        {"code": w.get("code", 0), "message": w.get("msg", "")}
        for w in trailer.get("warnings") or []
        if isinstance(w, dict)
    ]
    metrics = trailer.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    return {
        "warnings": warnings,
        "metrics": {
            "elapsed_time_ms": _sdk_ms(metrics.get("elapsedTime", "0")),
            "execution_time_ms": _sdk_ms(metrics.get("executionTime", "0")),
            "result_count": metrics.get("resultCount") or 0,
            "result_size": metrics.get("resultSize") or 0,
            "processed_objects": metrics.get("processedObjects") or 0,
        },
    }


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


class _ByteReader:
    """File-like ``read()`` over an httpx byte iterator, for ``ijson``."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._buffer = b""

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buffer) < size:
            chunk = next(self._chunks, None)
            if chunk is None:
                break
            self._buffer += chunk
        if size < 0:
            out, self._buffer = self._buffer, b""
        else:
            out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out


#: Top-level keys after ``results`` that are kept: metadata, plus ``errors``
#: so a query that failed after producing rows is never reported as success.
_TRAILER_KEYS = ("metrics", "warnings", "errors")


def _rows_then_trailer(
    events: Iterator[tuple[str, str, Any]], trailer: dict[str, Any]
) -> Iterator[Any]:
    """Yield each element of the top-level ``results`` array as it completes.

    Rows are built straight from parser events (one parse per row; the SDK
    re-serializes each row and parses it again). Containers are tracked by
    nesting depth rather than by prefix, so a row with a key that happens to
    be named ``item`` cannot end a row early. ``_TRAILER_KEYS`` objects are
    collected into ``trailer`` as the stream passes them.

    Raises ``OperationalInsightsRestError`` at the end of the stream if the
    server sent an ``errors`` array.
    """
    builder: ijson.ObjectBuilder | None = None
    target: str | None = None  # "row" or a trailer key
    depth = 0
    for prefix, event, value in events:
        if builder is None:
            is_start = event in ("start_map", "start_array")
            if prefix == "results.item":
                if not is_start:
                    yield value  # a scalar row
                    continue
                builder, target = ijson.ObjectBuilder(), "row"
            elif prefix in _TRAILER_KEYS and is_start:
                builder, target = ijson.ObjectBuilder(), prefix
            else:
                continue
        builder.event(event, value)
        if event in ("start_map", "start_array"):
            depth += 1
        elif event in ("end_map", "end_array"):
            depth -= 1
            if depth == 0:
                if target == "row":
                    yield builder.value
                else:
                    trailer[target] = builder.value  # type: ignore[index]
                builder, target = None, None
    if trailer.get("errors"):
        raise OperationalInsightsRestError(_format_errors(trailer["errors"]))


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryStatus:
    """One status check. ``result_path`` is set exactly when ``ready``."""

    ready: bool
    result_path: str | None = None


class OperationalInsightsRestClient:
    """httpx-based client for the async request API. Holds no query state.

    One ``httpx.Client`` is shared for its connection pool, which is the only
    thing kept between calls. It is thread-safe, and the tools run on
    FastMCP's thread pool.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        username: str,
        password: str,
        timeout_seconds: float,
        verify: bool | ssl.SSLContext = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise OperationalInsightsRestError(
                f"connection_string must be an http(s) URL; got {endpoint!r}."
            )
        # Scheme + host + port only, as the SDK does: every request path is
        # absolute (the server's handles already are).
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._client = httpx.Client(
            auth=(username, password),
            timeout=httpx.Timeout(timeout_seconds),
            verify=verify,
            transport=transport,
        )
        # Indirection so tests can skip real sleeping between retries.
        self._sleep = time.sleep

    @classmethod
    def from_settings(
        cls, settings: Mapping[str, Any], *, timeout_seconds: float
    ) -> OperationalInsightsRestClient:
        """Build a client from the server's resolved settings."""
        endpoint = settings.get("connection_string")
        if not endpoint:
            raise OperationalInsightsRestError(
                "Missing Operational Insights credentials: connection_string. "
                "Set it via --connection-string or "
                "CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING."
            )
        if settings.get("client_cert_path"):
            raise OperationalInsightsRestError(
                "Client certificate authentication is not supported by the "
                "REST-backed async query tools yet; configure a username and "
                "password, or use the SDK-backed tools."
            )
        username, password = settings.get("username"), settings.get("password")
        missing = [
            n for n, v in (("username", username), ("password", password)) if not v
        ]
        if missing:
            raise OperationalInsightsRestError(
                f"Missing Operational Insights credentials: {', '.join(missing)}. "
                "Set them via --username/--password or "
                "CB_OPERATIONAL_INSIGHTS_USERNAME/CB_OPERATIONAL_INSIGHTS_PASSWORD."
            )
        verify: bool | ssl.SSLContext = True
        ca_cert_path = settings.get("ca_cert_path")
        if ca_cert_path and str(endpoint).startswith("https://"):
            verify = ssl.create_default_context(cafile=ca_cert_path)
        return cls(
            endpoint,
            username=username,  # type: ignore[arg-type]
            password=password,  # type: ignore[arg-type]
            timeout_seconds=timeout_seconds,
            verify=verify,
        )

    def close(self) -> None:
        self._client.close()

    # -- transport --------------------------------------------------------

    def _send(
        self, method: str, path: str, *, stream: bool = False, **kwargs: Any
    ) -> httpx.Response:
        """Send one request, retrying what the SDK retries.

        Retried, with full-jitter exponential backoff:
        * connect failures (the request never reached the server), except
          TLS errors, which will not fix themselves;
        * HTTP 503;
        * a JSON body whose errors are *all* marked ``retriable`` (only
          checked for non-streamed responses: a streamed result body is
          read for rows, and its errors are handled there).

        Not retried: read/write/pool timeouts (the server may already be
        executing the request, and re-sending a start could run the
        statement twice), and every other response — those are returned and
        mapped to errors by the caller.
        """
        url = f"{self._origin}{path}"
        deadline = time.monotonic() + REST_RETRY_BUDGET_SECONDS
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.send(
                    self._client.build_request(method, url, **kwargs), stream=stream
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if "SSL" in str(e):
                    raise OperationalInsightsRestError(
                        f"TLS connection error: {e}"
                    ) from None
                failure = (
                    f"Could not connect to {self._origin} ({type(e).__name__}: {e})"
                )
            except httpx.TimeoutException as e:
                raise OperationalInsightsRestError(
                    f"Timed out waiting for Operational Insights ({type(e).__name__})."
                ) from None
            except httpx.HTTPError as e:
                raise OperationalInsightsRestError(
                    f"HTTP error talking to Operational Insights: {type(e).__name__}: {e}"
                ) from None
            else:
                failure = self._retriable_failure(resp, stream=stream)
                if failure is None:
                    return resp
                resp.close()

            delay = random.uniform(  # noqa: S311  # nosec B311 - backoff jitter, not security
                0,
                min(
                    REST_BACKOFF_MAX_SECONDS,
                    REST_BACKOFF_MIN_SECONDS * 2 ** (attempt - 1),
                ),
            )
            if attempt > REST_MAX_RETRIES or time.monotonic() + delay > deadline:
                suffix = f" (gave up after {attempt} attempts)" if attempt > 1 else ""
                raise OperationalInsightsRestError(f"{failure}{suffix}")
            logger.debug(
                f"Retrying {method} {path} in {delay:.2f}s "
                f"(attempt {attempt} of {REST_MAX_RETRIES + 1}): {failure}"
            )
            self._sleep(delay)

    @staticmethod
    def _retriable_failure(resp: httpx.Response, *, stream: bool) -> str | None:
        """Why this response should be retried, or ``None`` if it should not."""
        if resp.status_code == 503:
            if stream:
                resp.read()
            body = _response_json(resp)
            if isinstance(body, dict) and body.get("errors"):
                return _format_errors(body["errors"])
            return "Service unavailable (HTTP 503)."
        if not stream:
            body = _response_json(resp)
            if isinstance(body, dict) and _all_retriable(body.get("errors")):
                return _format_errors(body["errors"])
        return None

    # -- the four operations ----------------------------------------------

    def start_query(self, statement: str, *, readonly: bool) -> tuple[str, str]:
        """Submit an async query. Returns ``(request_id, status_path)``.

        Sends the same body the SDK does, except that ``readonly`` really is
        sent as ``readonly``: the SDK tools pass ``QueryOptions`` to
        ``start_query``, which only recognises ``StartQueryOptions``, so their
        read-only flag ends up as a positional query parameter instead.
        """
        body: dict[str, Any] = {
            "statement": statement,
            "client_context_id": str(uuid.uuid4()),
            "timeout": REST_QUERY_TIMEOUT,
            "mode": "async",
        }
        if readonly:
            body["readonly"] = True
        resp = self._send("POST", REQUEST_PATH, json=body)
        data = _raise_for_error_body(resp, "start query")
        request_id, handle = data.get("requestID"), data.get("handle")
        missing = [
            n for n, v in (("requestID", request_id), ("handle", handle)) if not v
        ]
        if missing:
            raise OperationalInsightsRestError(
                f"Server response is missing required field(s): {', '.join(missing)}."
            )
        return str(request_id), _server_path(str(handle), STATUS_PATH_PREFIX)

    def fetch_status(self, token: QueryToken) -> QueryStatus:
        """Check readiness. Raises on a failed query or an unknown handle."""
        resp = self._send("GET", token.status_path)
        if resp.status_code == 404:
            raise UnknownQueryHandleError()
        data = _raise_for_error_body(resp, "status check")
        handle = data.get("handle")
        if handle:
            return QueryStatus(True, _server_path(str(handle), RESULT_PATH_PREFIX))
        status = data.get("status")
        if status in FAILED_STATUSES:
            raise OperationalInsightsRestError(f"Query {status}.")
        return QueryStatus(False)

    def fetch_results(
        self, result_path: str, *, limit_bytes: int | None, service: str
    ) -> tuple[BoundedRows, dict[str, Any]]:
        """Stream a ready result through the size budget.

        Returns the bounded rows and the ``metadata`` block. A broken stream
        is re-fetched once from the start (see the module docstring).
        """
        for attempt in (1, 2):
            try:
                return self._fetch_results_once(
                    result_path, limit_bytes=limit_bytes, service=service
                )
            except _StreamBrokenError as e:
                if attempt == 2:
                    raise OperationalInsightsRestError(
                        f"The result download was cut off ({e}). The results "
                        f"are still on the server; call this tool again to retry."
                    ) from None
                logger.warning(f"Result stream broke ({e}); re-fetching from the start")
        raise AssertionError("unreachable")  # pragma: no cover

    def _fetch_results_once(
        self, result_path: str, *, limit_bytes: int | None, service: str
    ) -> tuple[BoundedRows, dict[str, Any]]:
        resp = self._send("GET", result_path, stream=True)
        try:
            if resp.status_code == 404:
                raise UnknownQueryHandleError()
            if resp.status_code >= 400:
                resp.read()
                _raise_for_error_body(resp, "result fetch")
            trailer: dict[str, Any] = {}
            events = ijson.parse(_ByteReader(resp.iter_bytes()), use_float=True)
            rows = _rows_then_trailer(events, trailer)
            try:
                bounded = collect_rows_within_budget(
                    rows, limit_bytes=limit_bytes, service=service
                )
            except (ijson.JSONError, httpx.RemoteProtocolError, httpx.ReadError) as e:
                raise _StreamBrokenError(f"{type(e).__name__}: {e}") from None
            except httpx.TimeoutException:
                raise OperationalInsightsRestError(
                    "Timed out waiting for result data from Operational Insights: "
                    "the server stopped sending mid-result."
                ) from None
            finally:
                rows.close()
            return bounded, build_metadata(trailer, complete=not bounded.truncated)
        finally:
            resp.close()

    def discard_results(self, result_path: str) -> None:
        """Free a ready result. 404 counts as done, as in the SDK."""
        resp = self._send("DELETE", result_path)
        if resp.status_code == 404 or 200 <= resp.status_code < 300:
            return
        _raise_for_error_body(resp, "discard")

    def cancel(self, request_id: str) -> None:
        """Cancel a running query. 404 counts as done, as in the SDK."""
        resp = self._send(
            "DELETE", ACTIVE_REQUESTS_PATH, data={"request_id": request_id}
        )
        if resp.status_code == 404 or 200 <= resp.status_code < 300:
            return
        _raise_for_error_body(resp, "cancel")
