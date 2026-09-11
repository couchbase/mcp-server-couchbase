"""Mock helpers shaped like the ``acouchbase`` API.

The async SDK is not uniformly awaitable, and mocking it as if it were is the
easiest way to write a test that passes for the wrong reason:

- ``cluster.bucket(name)`` and ``scope.query(...)`` are **synchronous** calls.
  ``bucket()`` returns a bucket; ``query()`` returns a request object that is
  then iterated with ``async for``.
- ``bucket.on_connect()``, the KV operations, the management APIs and
  ``ping``/``diagnostics``/``cluster_info`` are coroutines.

These helpers encode that split so tests exercise the same shape the real SDK
presents.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock


class AsyncRows:
    """Stand-in for an N1QL request object: iterated, never awaited."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = list(rows or [])

    def __aiter__(self):
        async def gen():
            for row in self._rows:
                yield row

        return gen()


def async_rows(rows: list[Any] | None = None) -> AsyncRows:
    """Build a query result that yields ``rows`` under ``async for``."""
    return AsyncRows(rows)


def make_bucket(**kwargs: Any) -> MagicMock:
    """A bucket mock whose ``on_connect`` is awaitable, as the tools require."""
    bucket = MagicMock(**kwargs)
    bucket.on_connect = AsyncMock()
    bucket.ping = AsyncMock()
    bucket.collections.return_value = AsyncMock()
    return bucket


def make_cluster(**kwargs: Any) -> MagicMock:
    """A cluster mock with the right sync/async split.

    ``bucket()`` and ``query()`` stay synchronous; everything the tools await is
    an ``AsyncMock``.
    """
    cluster = MagicMock(**kwargs)
    cluster.on_connect = AsyncMock()
    cluster.close = AsyncMock()
    cluster.ping = AsyncMock()
    cluster.diagnostics = AsyncMock()
    cluster.cluster_info = AsyncMock()
    cluster.wait_until_ready = AsyncMock()
    cluster.query.return_value = async_rows([])
    cluster.buckets.return_value = AsyncMock()
    # bucket() is sync but the bucket it yields must have an awaitable
    # on_connect(), which connect_to_bucket() awaits on every call.
    cluster.bucket.return_value = make_bucket()
    return cluster


def make_scope(**kwargs: Any) -> MagicMock:
    """A scope mock; ``query()`` is sync and returns an async-iterable."""
    scope = MagicMock(**kwargs)
    scope.query.return_value = async_rows([])
    return scope


def make_collection(**kwargs: Any) -> AsyncMock:
    """A collection mock: KV operations are awaited, accessors are not.

    ``query_indexes()`` is a synchronous accessor that hands back a manager
    whose own methods are coroutines — blanket-AsyncMocking the collection
    would make the accessor itself awaitable and the tools would never reach
    the manager.
    """
    collection = AsyncMock(**kwargs)
    collection.query_indexes = MagicMock(return_value=AsyncMock())
    return collection
