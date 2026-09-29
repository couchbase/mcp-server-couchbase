"""The audit record and its JSON-Lines serialisation.

One immutable record per line, matching Couchbase Server and Sync Gateway:
the same field vocabulary (``id``, ``name``, ``description``, ``timestamp``,
``real_userid``, ``cid``, ``outcome``) with identical semantics, so a customer
already running Couchbase auditing gets one format across the estate.

Key order is fixed rather than incidental. Audit files are read by people as
well as machines, and a stable prefix (id, name, description, timestamp, who,
where, correlation) means a human can scan a file without a parser. ``None``
values are omitted entirely so absence is unambiguous — notably ``reason``,
which the PRD specifies is present only when ``outcome`` is not ``success``.

Correlation has one scope: ``cid`` groups the records of one request. There is
deliberately no session identifier — MCP is moving to a stateless model in
which connection identity must not be read as session continuity.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any

_PACKAGE_NAME = "couchbase-mcp-server"

#: MCP-layer dispositions, per the PRD.
OUTCOME_SUCCESS = "success"
OUTCOME_DENIED = "denied"
OUTCOME_BLOCKED = "blocked"
OUTCOME_ERROR = "error"

#: Reasons recorded on a non-success tool call.
REASON_EXECUTION_ERROR = "execution_error"
REASON_INVALID_ARGUMENTS = "invalid_arguments"

#: Reason recorded when a bearer token is rejected. Deliberately a single
#: value: FastMCP's JWT verifier collapses expiry, signature, audience and
#: issuer failures into one ``None`` return, so a finer taxonomy would have to
#: be re-derived by decoding the token a second time. Out of phase-1 scope.
REASON_TOKEN_INVALID = "token_invalid"  # noqa: S105 - an audit reason, not a credential


def _server_version() -> str:
    try:
        return version(_PACKAGE_NAME)
    except PackageNotFoundError:  # pragma: no cover - editable/source checkouts
        return "unknown"


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def utc_timestamp() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SS.mmmZ``.

    Millisecond precision with a literal ``Z``, matching the PRD's samples and
    Sync Gateway's audit format. ``datetime.isoformat`` would emit microseconds
    and a ``+00:00`` offset, so the value is assembled explicitly.
    """
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{now.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class ServerContext:
    """Static per-process context, resolved once at startup."""

    version: str
    host: str

    @classmethod
    def detect(cls) -> ServerContext:
        return cls(version=_server_version(), host=_hostname())

    def as_dict(self) -> dict[str, str]:
        return {"version": self.version, "host": self.host}


@dataclass
class AuditRecord:
    """A single audit event.

    Args:
        id: Catalogue event id.
        name: Catalogue event name.
        description: Catalogue event description.
        outcome: MCP-layer disposition.
        real_userid: ``{"domain": ..., "user": ...}`` access-decision identity.
            Omitted for records that are not attributable to a caller, such as
            server start.
        cid: Server-minted correlation id joining the records of one request.
        reason: Present only when ``outcome`` is not ``success``.
        payload: Event-specific fields, merged in after the fixed prefix.
    """

    id: int
    name: str
    description: str
    outcome: str
    real_userid: dict[str, str] | None = None
    cid: str | None = None
    reason: str | None = None
    timestamp: str = field(default_factory=utc_timestamp)
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, server: ServerContext) -> dict[str, Any]:
        """Materialise the record in wire order, dropping absent fields."""
        document: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "timestamp": self.timestamp,
        }
        if self.real_userid is not None:
            document["real_userid"] = self.real_userid
        document["server"] = server.as_dict()
        if self.cid is not None:
            document["cid"] = self.cid
        document["outcome"] = self.outcome
        if self.reason is not None:
            document["reason"] = self.reason
        for key, value in self.payload.items():
            if value is not None:
                document[key] = value
        return document

    def to_json_line(self, server: ServerContext) -> str:
        """Serialise to a single JSON line, newline included.

        ``default=str`` guarantees a line is always produced: tool arguments
        can contain arbitrary caller-supplied values, and a record that failed
        to serialise would be a silently missing audit entry — strictly worse
        than a stringified value. ``ensure_ascii=False`` keeps non-Latin
        document ids and keyspace names readable.
        """
        return (
            json.dumps(
                self.as_dict(server),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            + "\n"
        )


__all__ = [
    "OUTCOME_BLOCKED",
    "OUTCOME_DENIED",
    "OUTCOME_ERROR",
    "OUTCOME_SUCCESS",
    "REASON_EXECUTION_ERROR",
    "REASON_INVALID_ARGUMENTS",
    "REASON_TOKEN_INVALID",
    "AuditRecord",
    "ServerContext",
    "utc_timestamp",
]
