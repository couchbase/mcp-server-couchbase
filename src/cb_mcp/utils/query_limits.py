"""Bounded, streaming collection of query rows.

Every query tool in this package used to buffer its entire result set before
returning: the operational server with ``for row in result: results.append(row)``,
both Operational Insights tools with ``result.get_all_rows()``. Both SDKs stream
rows lazily — the operational SDK through ``QueryResult.__iter__``, the
Operational Insights SDK through ``BlockingIterator`` over an incremental JSON
parser — so that buffering threw away the one property that made a large result
set survivable, and the server's peak memory was whatever the cluster chose to
send.

:func:`collect_rows_within_budget` is the single place that consumes a row
stream. It pulls rows one at a time, measures each one's serialized size, and
stops as soon as the next row would exceed the budget. The rows already read
are returned along with a flag saying the result is incomplete.

Why serialized JSON bytes rather than a row count or ``sys.getsizeof``: the
budget exists to bound the payload handed to an MCP client and, through it, the
model's context window. A row count cannot do that (rows vary by orders of
magnitude) and in-memory size correlates poorly with encoded size. The cost is
one ``json.dumps`` per row, which is work the MCP framework would do anyway when
it serializes the response — this just does it early enough to act on.

Service-agnostic by construction: it takes an iterable of rows, so the
operational SDK's ``QueryResult``, the Operational Insights SDK's
``BlockingQueryResult.rows()``, and a plain list in a test all work unchanged.
The ``service`` argument names the caller only for logging — it selects no
behaviour, because there is none to select.
"""

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .constants import (
    DEFAULT_MAX_QUERY_RESULT_SIZE,
    LOGGER_NAMESPACE,
    MAX_MAX_QUERY_RESULT_SIZE,
    MIN_MAX_QUERY_RESULT_SIZE,
)

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.utils.query_limits")

#: Settings key holding the resolved budget. Read through
#: :func:`max_query_result_size_from` rather than indexed directly, so a host
#: that never set it still gets the default.
MAX_QUERY_RESULT_SIZE_KEY = "max_query_result_size"

#: Sentinel for "the iterator had nothing more", distinct from a legitimate
#: ``None`` row.
_EXHAUSTED = object()


@dataclass(frozen=True)
class BoundedRows:
    """Rows read from a query stream, and whether the stream was cut short.

    ``truncated`` is the whole point of the type: a bare list cannot say
    whether it is the complete result or merely the part that fit, and a
    caller that cannot tell will present partial data as complete.
    """

    rows: list[Any]
    truncated: bool
    #: Serialized size of ``rows``, in bytes. Within ``limit_bytes`` except
    #: when a single oversized first row was kept — see
    #: :func:`collect_rows_within_budget`.
    bytes_returned: int
    #: The budget this collection ran under, echoed so callers can report it
    #: without re-reading settings.
    limit_bytes: int

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def truncation_message(self) -> str | None:
        """Operator/model-facing explanation, or ``None`` when complete.

        Phrased to tell the model two things it would otherwise get wrong:
        the remaining rows are *gone* (there is no cursor to resume from —
        unlike a paginated read), and the fix is to narrow the query rather
        than to call again.
        """
        if not self.truncated:
            return None
        return (
            f"Result truncated: returned the first {self.row_count} row(s) "
            f"({self.bytes_returned} bytes), reaching the "
            f"{self.limit_bytes}-byte limit. The remaining rows were not read "
            f"and cannot be retrieved by calling again — narrow the query with "
            f"a LIMIT, fewer projected fields, or a more selective WHERE "
            f"clause. An operator can raise the limit with "
            f"CB_MCP_MAX_QUERY_RESULT_SIZE."
        )

    def as_envelope_fields(self) -> dict[str, Any]:
        """The truncation half of a tool's success envelope.

        Returns ``{"truncated": False}`` when complete, so the key is always
        present: a model that has to infer completeness from an absent key
        will assume completeness, which is exactly the failure this guards.
        """
        if not self.truncated:
            return {"truncated": False}
        return {
            "truncated": True,
            "truncation": {
                "bytes_returned": self.bytes_returned,
                "limit_bytes": self.limit_bytes,
                "message": self.truncation_message(),
            },
        }


def clamp_max_query_result_size(value: int) -> int:
    """Clamp a configured budget into the supported range, warning if it moved.

    Clamping rather than raising is a deliberate deployment choice: see
    ``MAX_MAX_QUERY_RESULT_SIZE`` in ``constants``. The warning names both the
    requested and effective values so an operator reading the log is never
    misled about which one is in force.
    """
    if value > MAX_MAX_QUERY_RESULT_SIZE:
        logger.warning(
            f"max_query_result_size {value} exceeds the maximum "
            f"{MAX_MAX_QUERY_RESULT_SIZE}; clamping to "
            f"{MAX_MAX_QUERY_RESULT_SIZE}."
        )
        return MAX_MAX_QUERY_RESULT_SIZE
    if value < MIN_MAX_QUERY_RESULT_SIZE:
        logger.warning(
            f"max_query_result_size {value} is below the minimum "
            f"{MIN_MAX_QUERY_RESULT_SIZE}; clamping to "
            f"{MIN_MAX_QUERY_RESULT_SIZE}."
        )
        return MIN_MAX_QUERY_RESULT_SIZE
    return value


def max_query_result_size_from(settings: Any) -> int:
    """Read the budget out of a settings mapping, falling back to the default.

    Tolerant of a missing or ``None`` value because an embedding host builds
    its own settings mapping and may predate this key; such a host gets the
    default rather than a ``KeyError`` at query time. The value is clamped on
    read as well as at startup, since that host never passed through the CLI.
    """
    try:
        value = settings.get(MAX_QUERY_RESULT_SIZE_KEY)
    except AttributeError:
        value = None
    if value is None:
        return DEFAULT_MAX_QUERY_RESULT_SIZE
    try:
        return clamp_max_query_result_size(int(value))
    except (TypeError, ValueError):
        logger.warning(
            f"Ignoring non-numeric {MAX_QUERY_RESULT_SIZE_KEY} {value!r}; "
            f"using {DEFAULT_MAX_QUERY_RESULT_SIZE}."
        )
        return DEFAULT_MAX_QUERY_RESULT_SIZE


def max_query_result_size_for(ctx: Any) -> int:
    """The budget for this request, from the lifespan context.

    Reaches the settings mapping with ``getattr`` rather than
    ``get_settings``, matching ``get_logging_config`` and ``get_server_id`` in
    ``utils.context``: an embedding host may supply a lifespan-context type
    that carries no ``settings`` at all, and a *limit* lookup must never be
    the thing that fails an otherwise valid query. Such a host gets the
    default budget, which is the safe direction to err.
    """
    lifespan = getattr(getattr(ctx, "request_context", None), "lifespan_context", None)
    return max_query_result_size_from(getattr(lifespan, "settings", None))


def _encoded_size(row: Any) -> int:
    """Serialized size of one row in bytes.

    ``default=str`` mirrors what a JSON encoder must do with the non-JSON
    values a row can carry (``datetime``, ``Decimal``): this is a measurement,
    so it must not raise on a row the tool would still return. A row that
    cannot be measured at all is charged a nominal size rather than zero, so a
    stream of unmeasurable rows still terminates.
    """
    try:
        return len(json.dumps(row, default=str).encode("utf-8"))
    except Exception:
        logger.debug("Could not serialize a row for size accounting", exc_info=True)
        return 1


def collect_rows_within_budget(
    rows: Iterable[Any],
    *,
    limit_bytes: int,
    service: str,
) -> BoundedRows:
    """Read from ``rows`` until the serialized budget is reached.

    Stops at the first row that would push the total past ``limit_bytes``, and
    does not read beyond it: the underlying iterator is abandoned mid-stream,
    which is what keeps the cluster from shipping — and this process from
    buffering — the rest of the result.

    The first row is always kept, even when it alone exceeds the budget. A
    single oversized row is the one case where returning nothing would be
    strictly less useful than overshooting: the caller learns what the data
    looks like and that it was truncated, instead of an empty list that reads
    like "no matches". ``bytes_returned`` may therefore exceed ``limit_bytes``
    in that one case, and only that one.

    ``service`` appears only in the log line that records a truncation.
    """
    iterator: Iterator[Any] = iter(rows)
    collected: list[Any] = []
    total = 0
    truncated = False

    for row in iterator:
        size = _encoded_size(row)
        if collected and total + size > limit_bytes:
            truncated = True
            break
        collected.append(row)
        total += size
        if total >= limit_bytes:
            # The budget is spent. Probe for one more row rather than assuming
            # there is one: stopping here unconditionally would report a
            # complete result as truncated whenever it landed exactly on the
            # limit, which is the common case for a query whose rows divide
            # evenly into the budget.
            #
            # ``total > limit_bytes`` means the kept-anyway first row overran
            # the budget on its own, which is a truncation in its own right:
            # the caller asked for at most this many bytes and is getting more
            # of a row than it bargained for, with no guarantee it is whole.
            truncated = (
                next(iterator, _EXHAUSTED) is not _EXHAUSTED or total > limit_bytes
            )
            break

    if truncated:
        logger.warning(
            f"{service} query result truncated at {total} byte(s) "
            f"({len(collected)} row(s)); limit is {limit_bytes} byte(s)."
        )

    return BoundedRows(
        rows=collected,
        truncated=truncated,
        bytes_returned=total,
        limit_bytes=limit_bytes,
    )
