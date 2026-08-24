"""Server-side registry for async query handles.

Why this exists
---------------
Each MCP tool call is a separate function invocation, and the MCP client can
only carry a *string* between calls (in the tool JSON args). But the EA async
API works through live ``QueryHandle`` / ``QueryResultHandle`` Python objects
that wrap a live HTTP client + thread pool — they cannot be serialized (they
hold a ``_thread.RLock``) and therefore cannot be handed to the client or to a
different process.

So we keep the *live objects* in this server-side registry, keyed by an opaque
UUID token, and return only the token to the client. Later tool calls send the
token back and we look the live object up. The token is a coat-check ticket;
the server holds the coat.

Scope / limitations
-------------------
The registry lives in one process's memory (created once at lifespan startup and
attached to ``AppContext`` — never a module global). That makes it correct for:

    * stdio (always a single process), and
    * HTTP with a single replica.

It is NOT sufficient for multi-replica HTTP or restart-survival: a token minted
on replica A is unknown to replica B, and a restart wipes the map. For those
deployments the tools would need a *stateless* backend that carries EA's
server-side request-id/handle strings and rebuilds the REST calls each time
(no live object retained). This class is deliberately small and self-contained
so such a backend can replace it behind the same method surface.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any


class UnknownHandleError(KeyError):
    """Raised when a query_handle token is not found in the registry.

    Happens when the token is wrong, already discarded/cancelled, or was minted
    by a different server process (e.g. another replica, or before a restart).
    """


@dataclass
class _Entry:
    """One tracked async query."""

    handle: Any  # BlockingQueryHandle
    statement: str
    # The result handle is only available once results are ready; we cache it
    # the first time a status call reports readiness so fetch/discard can reuse
    # it without re-deriving it.
    result_handle: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class HandleRegistry:
    """Thread-safe map of opaque token -> live async query handle.

    Tool handlers run in FastMCP's thread pool, so access is guarded by a
    ``threading.Lock``.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def register(self, handle: Any, statement: str) -> str:
        """Store a live handle and return a fresh opaque token for it."""
        token = uuid.uuid4().hex
        with self._lock:
            self._entries[token] = _Entry(handle=handle, statement=statement)
        return token

    def get(self, token: str) -> _Entry:
        """Return the entry for a token, or raise ``UnknownHandleError``."""
        with self._lock:
            entry = self._entries.get(token)
        if entry is None:
            raise UnknownHandleError(
                f"Unknown query_handle '{token}'. It may be invalid, already "
                "discarded/cancelled, or created by a different server process."
            )
        return entry

    def set_result_handle(self, token: str, result_handle: Any) -> None:
        """Cache the result handle for a token once results are ready."""
        with self._lock:
            entry = self._entries.get(token)
            if entry is not None:
                entry.result_handle = result_handle

    def remove(self, token: str) -> None:
        """Evict a token (after discard or cancel). Idempotent."""
        with self._lock:
            self._entries.pop(token, None)

    def count(self) -> int:
        """Number of currently tracked queries (diagnostic)."""
        with self._lock:
            return len(self._entries)

    def list_entries(self) -> list[dict[str, Any]]:
        """Return a summary of every tracked query.

        Lets a caller recover query_handle tokens it has lost (e.g. the LLM
        dropped one out of its context). Includes the token, the statement, the
        EA request id, and whether the result handle has been cached yet.
        """
        with self._lock:
            return [
                {
                    "query_handle": token,
                    "statement": entry.statement,
                    "request_id": getattr(entry.handle, "_request_id", None),
                    "results_cached": entry.result_handle is not None,
                }
                for token, entry in self._entries.items()
            ]
