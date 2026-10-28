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
    LOGGER_NAMESPACE,
    MAX_MAX_QUERY_RESULT_SIZE,
    MIN_MAX_QUERY_RESULT_SIZE,
)

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.utils.query_limits")

#: Settings key holding the resolved budget. Read through
#: :func:`max_query_result_size_from` rather than indexed directly, so a host
#: that never set it still gets the default.
MAX_QUERY_RESULT_SIZE_KEY = "operational_insights_query_result_max_size"

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
    #: Serialized size of ``rows``, in bytes. Never exceeds ``limit_bytes``:
    #: the budget is a hard ceiling, so a row that would overrun it is
    #: dropped rather than returned.
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

        Zero rows gets its own wording. "Returned the first 0 row(s)" is
        technically accurate but reads like an empty result set, when the
        actual cause is the opposite — the very first row was too wide for
        the budget, so no amount of re-querying the same shape will help.
        """
        if not self.truncated:
            return None
        # At the hard maximum, raising the variable cannot help (values above it
        # are clamped), so don't send the reader to a setting that is a no-op.
        at_maximum = self.limit_bytes >= MAX_MAX_QUERY_RESULT_SIZE
        if self.row_count == 0:
            hint = (
                f"The limit is at its maximum of {MAX_MAX_QUERY_RESULT_SIZE} "
                f"bytes and cannot be raised; project fewer or smaller fields."
                if at_maximum
                else "Project fewer or smaller fields, or ask an operator to "
                "raise CB_MCP_OPERATIONAL_INSIGHTS_QUERY_RESULT_MAX_SIZE."
            )
            return (
                f"Result truncated before any row could be returned: the first "
                f"row alone exceeds the {self.limit_bytes}-byte limit. This is "
                f"not an empty result — matching rows exist but are too wide to "
                f"return. {hint}"
            )
        operator_hint = (
            f"The limit is at its maximum of {MAX_MAX_QUERY_RESULT_SIZE} bytes "
            f"and cannot be raised."
            if at_maximum
            else "An operator can raise the limit with "
            "CB_MCP_OPERATIONAL_INSIGHTS_QUERY_RESULT_MAX_SIZE."
        )
        return (
            f"Result truncated: returned the first {self.row_count} row(s) "
            f"({self.bytes_returned} bytes), reaching the "
            f"{self.limit_bytes}-byte limit. The remaining rows were not read "
            f"and cannot be retrieved by calling again — narrow the query with "
            f"a LIMIT, fewer projected fields, or a more selective WHERE "
            f"clause. {operator_hint}"
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

    Only an upper bound applies. ``0`` is a real budget of zero bytes and any
    positive value is honoured as given — a budget below one row's worth of
    JSON returns no rows, which ``truncation_message`` reports explicitly
    rather than letting it look like an empty result.

    Clamping rather than raising is a deliberate deployment choice: see
    ``MAX_MAX_QUERY_RESULT_SIZE`` in ``constants``. The warning names both the
    requested and effective values so an operator reading the log is never
    misled about which one is in force.
    """
    if value > MAX_MAX_QUERY_RESULT_SIZE:
        logger.warning(
            f"CB_MCP_OPERATIONAL_INSIGHTS_QUERY_RESULT_MAX_SIZE {value} exceeds "
            f"the maximum {MAX_MAX_QUERY_RESULT_SIZE}; clamping to "
            f"{MAX_MAX_QUERY_RESULT_SIZE}."
        )
        return MAX_MAX_QUERY_RESULT_SIZE
    if value < MIN_MAX_QUERY_RESULT_SIZE:
        logger.warning(
            f"CB_MCP_OPERATIONAL_INSIGHTS_QUERY_RESULT_MAX_SIZE {value} is below "
            f"the minimum {MIN_MAX_QUERY_RESULT_SIZE}; clamping to "
            f"{MIN_MAX_QUERY_RESULT_SIZE}."
        )
        return MIN_MAX_QUERY_RESULT_SIZE
    return value


def max_query_result_size_from(settings: Any) -> int | None:
    """Read the budget out of a settings mapping, or ``None`` when unset.

    ``None`` means *no truncation*: the result is returned whole, as it was
    before this budget existed. Capping results is an operator's decision, so
    an unconfigured server must not impose one.

    Tolerant of a missing key because an embedding host builds its own settings
    mapping and may predate it; such a host gets no truncation rather than a
    ``KeyError`` at query time. The value is clamped on read as well as at
    startup, since that host never passed through the CLI.
    """
    try:
        value = settings.get(MAX_QUERY_RESULT_SIZE_KEY)
    except AttributeError:
        value = None
    if value is None:
        return None
    try:
        return clamp_max_query_result_size(int(value))
    except (TypeError, ValueError):
        logger.warning(
            f"Ignoring non-numeric {MAX_QUERY_RESULT_SIZE_KEY} {value!r}; "
            f"no result-size limit will be applied."
        )
        return None


def max_query_result_size_for(ctx: Any) -> int | None:
    """The budget for this request, or ``None`` when no limit is configured.

    Reaches the settings mapping with ``getattr`` rather than
    ``get_settings``, matching ``get_logging_config`` and ``get_server_id`` in
    ``utils.context``: an embedding host may supply a lifespan-context type
    that carries no ``settings`` at all, and a *limit* lookup must never be
    the thing that fails an otherwise valid query.
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
    limit_bytes: int | None,
    service: str,
) -> BoundedRows:
    """Read from ``rows`` until the serialized budget is reached.

    Stops at the first row that would push the total past ``limit_bytes``, and
    does not read beyond it: the underlying iterator is abandoned mid-stream,
    which is what keeps the cluster from shipping — and this process from
    buffering — the rest of the result.

    The budget is a hard ceiling: ``bytes_returned`` never exceeds
    ``limit_bytes``. A single row larger than the whole budget is therefore
    *not* returned, and the result comes back empty with ``truncated`` set —
    an operator who caps the payload at N bytes must get at most N bytes, even
    when that means one wide row is unreachable until they raise the limit or
    project fewer fields.

    That makes an empty-but-truncated result meaningful, and distinct from an
    empty-and-complete one (a query that matched nothing): callers must read
    ``truncated`` rather than inferring emptiness means "no matches".

    ``limit_bytes`` of ``None`` means no limit: every row is returned and no
    row is measured, so an unconfigured server pays none of the per-row
    ``json.dumps`` cost.

    ``service`` appears only in the log line that records a truncation.
    """
    if limit_bytes is None:
        collected_all = list(rows)
        return BoundedRows(
            rows=collected_all,
            truncated=False,
            # Not measured: computing it would cost a json.dumps per row for a
            # number nothing reads when there is no budget to report against.
            bytes_returned=0,
            limit_bytes=0,
        )

    iterator: Iterator[Any] = iter(rows)
    collected: list[Any] = []
    total = 0
    truncated = False

    for row in iterator:
        size = _encoded_size(row)
        if total + size > limit_bytes:
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
            truncated = next(iterator, _EXHAUSTED) is not _EXHAUSTED
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
