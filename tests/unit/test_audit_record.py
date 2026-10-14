"""Tests for the audit record and its serialisation.

Coverage map:
- timestamp format is the PRD's millisecond UTC form
- fixed key order in the wire document
- absent fields are omitted rather than serialised as null
- reason appears only on non-success outcomes
- payload keys with a None value are dropped
- non-serialisable argument values never lose the whole record
- one JSON object per line, newline terminated
- the PRD's own sample records round-trip
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from cb_mcp.audit.record import (
    OUTCOME_DENIED,
    OUTCOME_SUCCESS,
    AuditRecord,
    ServerContext,
    utc_timestamp,
)

SERVER = ServerContext(version="1.1.0", host="mcp-node-3")


def test_timestamp_is_millisecond_utc_with_a_literal_z():
    stamp = utc_timestamp()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", stamp), stamp
    # Parseable back to a datetime, so downstream tooling can rely on it.
    datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ")


def test_key_order_is_stable_and_readable():
    record = AuditRecord(
        id=61490,
        name="document read",
        description="A document read operation was requested through a tool",
        outcome=OUTCOME_SUCCESS,
        real_userid={"domain": "local", "user": "cbmcp"},
        cid="req-1",
        payload={"tool_name": "get_document_by_id", "ks": "b.s.c"},
    )
    keys = list(record.as_dict(SERVER))
    assert keys == [
        "id",
        "name",
        "description",
        "timestamp",
        "real_userid",
        "server",
        "cid",
        "outcome",
        "tool_name",
        "ks",
    ]


def test_absent_fields_are_omitted_not_null():
    record = AuditRecord(
        id=57344, name="server started", description="d", outcome=OUTCOME_SUCCESS
    )
    document = record.as_dict(SERVER)
    assert "real_userid" not in document
    assert "cid" not in document
    assert "reason" not in document
    assert document["server"] == {"version": "1.1.0", "host": "mcp-node-3"}


def test_no_session_identifier_is_ever_written():
    """The audit format carries no session id, by design.

    MCP is moving to a stateless model in which connection identity must not be
    read as session continuity, so a request is the only correlation scope this
    format claims. Pinned as a test so a future field cannot be added without
    a deliberate, visible change here.
    """
    record = AuditRecord(
        id=57360,
        name="session initialized",
        description="An MCP session was initialized",
        outcome="success",
        real_userid={"domain": "local", "user": "cbmcp"},
        cid="req-9",
    )
    document = record.as_dict(SERVER)
    assert document["cid"] == "req-9"
    assert "sid" not in document
    assert not [key for key in document if "session" in key.lower()]


def test_reason_is_present_only_on_failure():
    denied = AuditRecord(
        id=57377,
        name="scope check denied",
        description="d",
        outcome=OUTCOME_DENIED,
        reason="missing_scope",
    )
    assert denied.as_dict(SERVER)["reason"] == "missing_scope"

    ok = AuditRecord(
        id=61490, name="document read", description="d", outcome=OUTCOME_SUCCESS
    )
    assert "reason" not in ok.as_dict(SERVER)


def test_none_payload_values_are_dropped():
    record = AuditRecord(
        id=61488,
        name="cluster read",
        description="d",
        outcome=OUTCOME_SUCCESS,
        payload={
            "tool_name": "test_cluster_connection",
            "ks": None,
            "cb_userid": None,
            "confirmation": None,
        },
    )
    document = record.as_dict(SERVER)
    assert document["tool_name"] == "test_cluster_connection"
    # A cluster-level tool addresses no keyspace; the key simply does not appear.
    assert "ks" not in document
    assert "cb_userid" not in document
    assert "confirmation" not in document


def test_to_json_line_is_one_object_terminated_by_a_newline():
    record = AuditRecord(id=1, name="n", description="d", outcome=OUTCOME_SUCCESS)
    line = record.to_json_line(SERVER)
    assert line.endswith("\n")
    assert "\n" not in line[:-1], "a record must never span lines"
    assert json.loads(line)["id"] == 1


def test_unserialisable_values_do_not_lose_the_record():
    """Caller-supplied arguments can contain anything.

    A record that failed to serialise would be a silently missing audit entry,
    which is strictly worse than a stringified value.
    """

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    record = AuditRecord(
        id=61522,
        name="document write",
        description="d",
        outcome=OUTCOME_SUCCESS,
        payload={"args": {"document_content": Opaque()}},
    )
    document = json.loads(record.to_json_line(SERVER))
    assert document["args"]["document_content"] == "<opaque>"


def test_non_ascii_values_are_preserved_not_escaped():
    record = AuditRecord(
        id=61490,
        name="document read",
        description="d",
        outcome=OUTCOME_SUCCESS,
        payload={"ks": "bücher.inventar.artikel"},
    )
    line = record.to_json_line(SERVER)
    assert "bücher" in line
    assert json.loads(line)["ks"] == "bücher.inventar.artikel"


def test_server_context_detect_populates_both_fields():
    detected = ServerContext.detect()
    assert detected.version
    assert detected.host


@pytest.mark.parametrize(
    ("record", "expected_subset"),
    [
        (
            # The PRD's "successful document read" sample.
            AuditRecord(
                id=61490,
                name="document read",
                description="A document was read by ID",
                outcome="success",
                real_userid={"domain": "local", "user": "cbmcp"},
                cid="req-5f2a",
                payload={
                    "tool_name": "get_document_by_id",
                    "required_scope": "read",
                    "ks": "travel-sample.inventory.airline",
                },
            ),
            {
                "id": 61490,
                "outcome": "success",
                "tool_name": "get_document_by_id",
                "required_scope": "read",
                "ks": "travel-sample.inventory.airline",
            },
        ),
        (
            # The PRD's "denied write" sample.
            AuditRecord(
                id=57377,
                name="scope check denied",
                description="A write tool was requested without write scope",
                outcome="denied",
                real_userid={"domain": "oauth", "user": "agent-svc-7"},
                cid="req-7c19",
                reason="missing_scope",
                payload={
                    "client_id": "agent-svc-7",
                    "scopes": ["couchbase-mcp:read"],
                    "required_scope": "write",
                },
            ),
            {
                "id": 57377,
                "outcome": "denied",
                "reason": "missing_scope",
                "client_id": "agent-svc-7",
                "scopes": ["couchbase-mcp:read"],
            },
        ),
    ],
)
def test_prd_sample_records_serialise(record, expected_subset):
    document = json.loads(record.to_json_line(SERVER))
    for key, value in expected_subset.items():
        assert document[key] == value


# ---------------------------------------------------------------------------
# one record is one line, whatever a caller puts in it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("char", "name"),
    [
        # The three that ``json.dumps`` leaves raw. Everything else
        # ``str.splitlines`` splits on is below 0x20, which it already escapes.
        ("\u2028", "LINE SEPARATOR"),
        ("\u2029", "PARAGRAPH SEPARATOR"),
        ("\x85", "NEXT LINE"),
    ],
)
def test_a_caller_cannot_split_a_record_into_two_lines(char, name):
    """JSON Lines means one record per line, and a caller must not break it.

    ``ensure_ascii=False`` leaves these as raw bytes, and Python's
    ``str.splitlines`` — used by this repo's own readers and by many log
    shippers — treats every one of them as a line terminator. A bucket or
    document name carrying one would split its own record into unparseable
    fragments, destroying the entry that recorded the call. Reachable without
    any cluster access: the record is written even when the tool fails.
    """
    record = AuditRecord(
        id=61490,
        name="document read",
        description="d",
        outcome=OUTCOME_SUCCESS,
        payload={"ks": f"travel{char}sample.inventory.airline"},
    )
    line = record.to_json_line(ServerContext.detect())

    assert line.endswith("\n")
    assert len(line.splitlines()) == 1, f"{name} split the record"
    # Escaped, not stripped: the recorded value still says what the caller sent.
    assert json.loads(line)["ks"] == f"travel{char}sample.inventory.airline"


def test_escaping_leaves_ordinary_non_ascii_readable():
    """The escape must not undo ``ensure_ascii=False`` for normal text."""
    record = AuditRecord(
        id=61490,
        name="document read",
        description="d",
        outcome=OUTCOME_SUCCESS,
        payload={"ks": "航空会社.inventory.航空"},
    )
    line = record.to_json_line(ServerContext.detect())

    assert "航空会社" in line, "non-Latin names must stay readable in the file"
    assert json.loads(line)["ks"] == "航空会社.inventory.航空"
