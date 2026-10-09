"""End-to-end tests for the audit middleware.

These run a real ``FastMCP`` server with the middleware installed and a real
:class:`AuditSink` writing to a temporary file, then read the file back. Nothing
is mocked between the tool call and the record on disk, because the behaviours
that matter here are exactly the ones a mock would paper over:

* FastMCP wraps a tool's exception in ``ToolError``, so a refusal has to travel
  to the middleware through the per-request audit state rather than through the
  exception.
* Synchronous tools run on a worker thread, so that state has to be a mutable
  object shared across a copied context rather than a rebound contextvar.

Coverage map:
- session-initialized record from the initialize handshake
- tool-call read and write records, ids and required_scope
- keyspace extraction from tool arguments
- success, execution error and result-reported error outcomes
- scope denial recorded as a core authorization event, with no tool-call event
- read-only SQL++ block recorded as a guardrail event
- confirmation declined and confirmation skipped
- SQL++ statement-driven read/write classification
- argument capture on and off
- per-event filtering
- correlation: one ``cid`` per request, readable from tool code, over the
  in-memory and real HTTP transports, and no session identifier anywhere
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import uvicorn
from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from cb_mcp.audit import state as audit_state
from cb_mcp.audit.catalog import AuditEvent
from cb_mcp.audit.classification import resolve_tool_call_event
from cb_mcp.audit.config import resolve_audit_config
from cb_mcp.audit.emitter import AuditLogger
from cb_mcp.audit.exceptions import ReadOnlyWriteBlockedError, ScopeDeniedError
from cb_mcp.audit.identity import resolve_real_userid
from cb_mcp.audit.middleware import AuditMiddleware, extract_keyspace
from cb_mcp.audit.record import OUTCOME_ERROR
from cb_mcp.audit.sink import AuditSink


def _build_logger(tmp_path: Path, **overrides) -> AuditLogger:
    """An active AuditLogger writing to a real file under ``tmp_path``."""
    options = {
        "enabled": True,
        "file": str(tmp_path / "audit.log"),
        "sinks": "file",
        "rotation_max_size_mb": 8.0,
        "rotation_interval": "0",
        "max_backups": 2,
        "tool_args": False,
        "disabled_events": None,
    }
    options.update(overrides)
    config = resolve_audit_config(**options)
    sink = AuditSink(
        config.file,
        max_bytes=config.max_bytes,
        max_backups=config.max_backups,
        interval_seconds=config.rotation_interval_seconds,
    )
    sink.start()
    return AuditLogger(config, sink)


def _records(audit: AuditLogger) -> list[dict]:
    """Close the sink so the writer thread drains, then parse every line."""
    path = audit._sink.path
    audit.close()
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _server(audit: AuditLogger, transport: str = "stdio") -> FastMCP:
    mcp = FastMCP("audit-test")
    mcp.add_middleware(
        AuditMiddleware(
            transport=transport, cb_userid="mcp_service", audit_logger=audit
        )
    )
    return mcp


# ---------------------------------------------------------------------------
# keyspace extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (None, None),
        ({}, None),
        ({"limit": 5}, None),
        ({"bucket_name": "travel-sample"}, "travel-sample"),
        ({"bucket_name": "b", "scope_name": "s"}, "b.s"),
        ({"bucket_name": "b", "scope_name": "s", "collection_name": "c"}, "b.s.c"),
        # A gap truncates rather than producing a misleading dotted name.
        ({"bucket_name": "b", "collection_name": "c"}, "b"),
        ({"scope_name": "s", "collection_name": "c"}, None),
        ({"bucket_name": "", "scope_name": "s"}, None),
    ],
)
def test_extract_keyspace(arguments, expected):
    assert extract_keyspace(arguments) == expected


# ---------------------------------------------------------------------------
# session + tool calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_initialized_is_recorded(tmp_path):
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    async with Client(mcp):
        pass

    records = _records(audit)
    sessions = [r for r in records if r["id"] == AuditEvent.SESSION_INITIALIZED.id]
    assert len(sessions) == 1
    record = sessions[0]
    assert record["name"] == "session initialized"
    assert record["outcome"] == "success"
    assert record["client_name"]
    assert record["protocol_version"]
    assert record["real_userid"]["domain"] == "local"
    assert record["cid"]


@pytest.mark.asyncio
async def test_read_tool_call_record(tmp_path):
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def get_document_by_id(
        ctx: Context, bucket_name: str, scope_name: str, collection_name: str
    ) -> str:
        """read a document"""
        return "doc"

    async with Client(mcp) as client:
        await client.call_tool(
            "get_document_by_id",
            {
                "bucket_name": "travel-sample",
                "scope_name": "inventory",
                "collection_name": "airline",
            },
        )

    calls = [r for r in _records(audit) if r["id"] == 61490]
    assert len(calls) == 1
    record = calls[0]
    assert record["name"] == "document read"
    assert record["outcome"] == "success"
    assert record["tool_name"] == "get_document_by_id"
    assert record["required_scope"] == "read"
    assert record["ks"] == "travel-sample.inventory.airline"
    assert record["cb_userid"] == "mcp_service"
    assert "reason" not in record
    # Arguments are not captured unless explicitly enabled.
    assert "args" not in record


@pytest.mark.asyncio
async def test_write_tool_call_record_is_a_write_id(tmp_path):
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def upsert_document_by_id(
        ctx: Context, bucket_name: str, scope_name: str, collection_name: str
    ) -> str:
        """write a document"""
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool(
            "upsert_document_by_id",
            {"bucket_name": "b", "scope_name": "s", "collection_name": "c"},
        )

    calls = [
        r for r in _records(audit) if r.get("tool_name") == "upsert_document_by_id"
    ]
    assert [r["id"] for r in calls] == [61522]
    assert calls[0]["required_scope"] == "write"


@pytest.mark.asyncio
async def test_a_cancelled_call_is_still_recorded(tmp_path):
    """A caller must not be able to erase a write record by cancelling.

    ``asyncio.CancelledError`` is a ``BaseException``, so an ``except
    Exception`` around the tool let it escape with no record at all. The tool
    may already have mutated the cluster by then — a client timeout, a dropped
    HTTP connection or a shutdown mid-call would leave the write invisible,
    and document-write ids are non-filterable precisely so they cannot be
    suppressed.
    """
    audit = _build_logger(tmp_path)
    mcp = _server(audit)
    mutated = {"done": False}

    @mcp.tool
    async def upsert_document_by_id(
        ctx: Context, bucket_name: str, scope_name: str, collection_name: str
    ) -> str:
        """mutates, then is slow to answer"""
        mutated["done"] = True
        await asyncio.sleep(30)
        return "never reached"

    async with Client(mcp) as client:
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(
                client.call_tool(
                    "upsert_document_by_id",
                    {
                        "bucket_name": "travel-sample",
                        "scope_name": "inventory",
                        "collection_name": "airline",
                    },
                ),
                timeout=0.5,
            )
        await asyncio.sleep(0.2)

    assert mutated["done"], "the probe never reached the mutation"
    records = _records(audit)
    writes = [r for r in records if r.get("tool_name") == "upsert_document_by_id"]
    assert writes, f"the cancelled write left no record: {[r['name'] for r in records]}"
    assert writes[0]["id"] == 61522, writes[0]
    assert writes[0]["outcome"] == OUTCOME_ERROR
    # Write ids are never filterable, so the record cannot be configured away
    # either.
    assert resolve_tool_call_event("upsert_document_by_id").filterable is False


@pytest.mark.asyncio
async def test_malformed_arguments_are_recorded_as_invalid_arguments(tmp_path):
    """Bad input and a failed execution are different facts.

    FastMCP validates arguments against the tool's schema before the body runs,
    so ``execution_error`` on one of those asserts an execution that never
    began — on a record that also carries a keyspace and a required scope taken
    from arguments the server refused. The PRD defines ``invalid_arguments``
    for exactly this; it was defined in the code and never emitted.
    """
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def get_document_by_id(
        ctx: Context, bucket_name: str, scope_name: str, collection_name: str
    ) -> str:
        """never reached"""
        raise AssertionError("the tool body must not run")

    async with Client(mcp) as client:
        with contextlib.suppress(Exception):
            # scope_name and collection_name missing, bucket_name the wrong type
            await client.call_tool("get_document_by_id", {"bucket_name": 123})

    records = [r for r in _records(audit) if r.get("tool_name") == "get_document_by_id"]
    assert records, "a rejected call left no record"
    assert records[0]["outcome"] == OUTCOME_ERROR
    assert records[0]["reason"] == "invalid_arguments", records[0]


@pytest.mark.asyncio
async def test_execution_error_outcome(tmp_path):
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def get_document_by_id(ctx: Context, bucket_name: str) -> str:
        """raises"""
        raise RuntimeError("cluster unreachable")

    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("get_document_by_id", {"bucket_name": "b"})

    calls = [r for r in _records(audit) if r["id"] == 61490]
    assert len(calls) == 1
    assert calls[0]["outcome"] == "error"
    assert calls[0]["reason"] == "execution_error"


@pytest.mark.asyncio
async def test_argument_capture_when_enabled(tmp_path):
    audit = _build_logger(tmp_path, tool_args=True)
    mcp = _server(audit)

    @mcp.tool
    def upsert_document_by_id(
        ctx: Context, bucket_name: str, document_content: dict
    ) -> str:
        """write"""
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool(
            "upsert_document_by_id",
            {"bucket_name": "b", "document_content": {"secret": "value"}},
        )

    calls = [
        r for r in _records(audit) if r.get("tool_name") == "upsert_document_by_id"
    ]
    # No redaction exists in this release: the body is recorded verbatim. This
    # asserts the documented behaviour so a future redaction change is a
    # deliberate, visible test update rather than a silent one.
    assert calls[0]["args"]["document_content"] == {"secret": "value"}


@pytest.mark.asyncio
async def test_unclassified_tool_fails_closed_to_a_write_id(tmp_path):
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def brand_new_tool(ctx: Context) -> str:
        """not in the classification map"""
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("brand_new_tool", {})

    calls = [r for r in _records(audit) if r.get("tool_name") == "brand_new_tool"]
    assert calls[0]["required_scope"] == "write"
    assert calls[0]["unclassified"] is True


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_denial_emits_authz_event_and_no_tool_call(tmp_path):
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def upsert_document_by_id(ctx: Context, bucket_name: str) -> str:
        """denied before it runs"""
        audit_state.record_refusal(
            event_id=ScopeDeniedError.audit_event.id,
            event_name=ScopeDeniedError.audit_event.event_name,
            outcome=ScopeDeniedError.audit_outcome,
            reason=ScopeDeniedError.audit_reason,
            required_scope=["couchbase-mcp:write"],
        )
        raise ScopeDeniedError("missing scope")

    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("upsert_document_by_id", {"bucket_name": "b"})

    records = _records(audit)
    authz = [r for r in records if r["id"] == AuditEvent.SCOPE_CHECK_DENIED.id]
    assert len(authz) == 1
    assert authz[0]["outcome"] == "denied"
    assert authz[0]["reason"] == "missing_scope"
    assert authz[0]["tool_name"] == "upsert_document_by_id"
    # The tool never ran, so there is no tool-call event to double-count it.
    assert not [r for r in records if r["id"] == 61522]


@pytest.mark.asyncio
async def test_read_only_block_emits_guardrail_event(tmp_path):
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def run_sql_plus_plus_query(ctx: Context, bucket_name: str, query: str) -> str:
        """blocked by read-only mode"""
        audit_state.record_statement_class("write")
        audit_state.record_refusal(
            event_id=ReadOnlyWriteBlockedError.audit_event.id,
            event_name=ReadOnlyWriteBlockedError.audit_event.event_name,
            outcome=ReadOnlyWriteBlockedError.audit_outcome,
            reason=ReadOnlyWriteBlockedError.audit_reason,
            statement_kind="data",
        )
        raise ReadOnlyWriteBlockedError("not allowed in read-only mode")

    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "run_sql_plus_plus_query", {"bucket_name": "b", "query": "DELETE ..."}
            )

    records = _records(audit)
    guardrail = [r for r in records if r["id"] == AuditEvent.WRITE_BLOCKED_READ_ONLY.id]
    assert len(guardrail) == 1
    assert guardrail[0]["outcome"] == "blocked"
    assert guardrail[0]["reason"] == "read_only_mode"
    assert guardrail[0]["statement_kind"] == "data"


@pytest.mark.asyncio
async def test_confirmation_skipped_emits_guardrail_and_tool_call(tmp_path):
    """The tool *did* run, so both records are produced and they share a cid."""
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def delete_document_by_id(ctx: Context, bucket_name: str) -> str:
        """runs without confirmation because the client cannot elicit"""
        audit_state.record_confirmation(audit_state.CONFIRMATION_SKIPPED)
        return "deleted"

    async with Client(mcp) as client:
        await client.call_tool("delete_document_by_id", {"bucket_name": "b"})

    records = _records(audit)
    skipped = [r for r in records if r["id"] == AuditEvent.CONFIRMATION_SKIPPED.id]
    calls = [r for r in records if r["id"] == 61522]
    assert len(skipped) == 1
    assert len(calls) == 1
    assert skipped[0]["reason"] == "confirmation_unsupported"
    assert calls[0]["confirmation"] == "skipped"
    assert calls[0]["outcome"] == "success"
    # Same request, so an investigator can join the two records.
    assert skipped[0]["cid"] == calls[0]["cid"]


@pytest.mark.asyncio
async def test_confirmation_skipped_is_not_claimed_when_the_call_was_blocked(tmp_path):
    """57491 asserts the tool executed. It must not fire when a gate stopped it.

    The confirmation wrapper sits outside the tool and the SQL++ gates sit
    inside it, so a call can skip confirmation and then be refused anyway.
    Emitting both left the trail saying a destructive tool ran unconfirmed
    *and* was blocked — and 57491 carries ``outcome=success``, so a reviewer
    counting unconfirmed executions would count one that never happened.
    """
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def run_sql_plus_plus_query(ctx: Context, bucket_name: str, query: str) -> str:
        """skips confirmation, then the read-only gate refuses it"""
        audit_state.record_confirmation(audit_state.CONFIRMATION_SKIPPED)
        audit_state.record_statement_class("write")
        audit_state.record_refusal(
            event_id=ReadOnlyWriteBlockedError.audit_event.id,
            event_name=ReadOnlyWriteBlockedError.audit_event.event_name,
            outcome=ReadOnlyWriteBlockedError.audit_outcome,
            reason=ReadOnlyWriteBlockedError.audit_reason,
            statement_kind="data",
        )
        raise ReadOnlyWriteBlockedError("not allowed in read-only mode")

    async with Client(mcp) as client:
        with contextlib.suppress(ToolError):
            await client.call_tool(
                "run_sql_plus_plus_query",
                {"bucket_name": "b", "query": "DELETE FROM airline"},
            )

    records = _records(audit)
    skipped = [r for r in records if r["id"] == AuditEvent.CONFIRMATION_SKIPPED.id]
    blocked = [r for r in records if r["id"] == AuditEvent.WRITE_BLOCKED_READ_ONLY.id]

    assert blocked, "the refusal itself must still be recorded"
    assert blocked[0]["outcome"] == "blocked"
    assert skipped == [], (
        "57491 claims the tool executed without confirmation, but it was blocked"
    )


@pytest.mark.asyncio
async def test_sqlpp_write_statement_uses_the_query_write_id(tmp_path):
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def run_sql_plus_plus_query(ctx: Context, bucket_name: str, query: str) -> str:
        """an allowed DML statement"""
        audit_state.record_statement_class("write")
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool(
            "run_sql_plus_plus_query", {"bucket_name": "b", "query": "UPDATE ..."}
        )

    calls = [
        r for r in _records(audit) if r.get("tool_name") == "run_sql_plus_plus_query"
    ]
    # 61523, not the 61491 read id: a successful mutation must not be filed as
    # a read, because read ids are filterable and could be suppressed.
    assert [r["id"] for r in calls] == [61523]
    assert calls[0]["required_scope"] == "write"


@pytest.mark.asyncio
async def test_disabled_event_is_not_written(tmp_path):
    audit = _build_logger(tmp_path, disabled_events="61490")  # document read
    mcp = _server(audit)

    @mcp.tool
    def get_document_by_id(ctx: Context, bucket_name: str) -> str:
        """filtered out"""
        return "doc"

    async with Client(mcp) as client:
        await client.call_tool("get_document_by_id", {"bucket_name": "b"})

    assert not [r for r in _records(audit) if r["id"] == 61490]


@pytest.mark.asyncio
async def test_inactive_logger_writes_nothing_and_does_not_break_calls(tmp_path):
    """With auditing off the middleware must be transparent."""
    config = resolve_audit_config(
        enabled=False,
        file=None,
        rotation_max_size_mb=None,
        max_backups=None,
        tool_args=None,
        disabled_events=None,
    )
    audit = AuditLogger(config, sink=None)
    mcp = _server(audit)

    @mcp.tool
    def get_document_by_id(ctx: Context, bucket_name: str) -> str:
        """still works"""
        return "doc"

    async with Client(mcp) as client:
        result = await client.call_tool("get_document_by_id", {"bucket_name": "b"})

    assert result is not None
    assert not audit.active
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_withheld_or_unknown_tool_produces_no_record(tmp_path):
    """A tool that was never registered must not be audited.

    Read-only mode and the disabled set both work by withholding the tool from
    registration, so a client is never told it exists and there is no refusal to
    record — that was the product decision behind dropping event 57489. Emitting
    a tool-call record here would also assert an ``execution_error`` that never
    happened, because nothing ran. The enforced tool surface is captured once by
    the server-configuration record at startup instead.
    """
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def get_document_by_id(ctx: Context, bucket_name: str) -> str:
        """the only registered tool"""
        return "doc"

    async with Client(mcp) as client:
        with pytest.raises(Exception, match="Unknown tool"):
            # Withheld in read-only mode, so absent from the registry.
            await client.call_tool("upsert_document_by_id", {"bucket_name": "b"})

    records = _records(audit)
    assert not [r for r in records if r.get("tool_name") == "upsert_document_by_id"]
    assert not [r for r in records if r["id"] == 61522]
    # The session record still lands, so the sink itself was working.
    assert [r for r in records if r["id"] == AuditEvent.SESSION_INITIALIZED.id]


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# correlation: one cid per request, and no session identifier at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_request_gets_its_own_cid(tmp_path):
    """A ``cid`` identifies one request, so two calls must never share one."""
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def get_document_by_id(
        ctx: Context, bucket_name: str, scope_name: str, collection_name: str
    ) -> str:
        """read a document"""
        return "doc"

    async with Client(mcp) as client:
        for _ in range(2):
            await client.call_tool(
                "get_document_by_id",
                {"bucket_name": "b", "scope_name": "s", "collection_name": "c"},
            )

    records = _records(audit)
    session = next(r for r in records if r["id"] == AuditEvent.SESSION_INITIALIZED.id)
    calls = [r for r in records if r["id"] == 61490]
    assert len(calls) == 2

    cids = [session["cid"], calls[0]["cid"], calls[1]["cid"]]
    assert all(cids), "every record must carry a cid"
    assert len(set(cids)) == 3, "three requests, three distinct cids"


@pytest.mark.asyncio
async def test_no_record_carries_a_session_identifier(tmp_path):
    """The audit log deliberately claims no session scope.

    MCP is moving to a stateless model in which a server must not treat
    connection or process identity as session continuity, so an id derived from
    it would group unrelated interleaved conversations. Asserted across a whole
    run rather than one record, so no emission path can reintroduce one.
    """
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def upsert_document_by_id(ctx: Context, bucket_name: str) -> str:
        """write a document"""
        return "ok"

    async with Client(mcp) as client:
        await client.call_tool("upsert_document_by_id", {"bucket_name": "b"})

    records = _records(audit)
    assert records
    for record in records:
        assert "sid" not in record
        assert not [key for key in record if "session" in key.lower()]


@pytest.mark.asyncio
async def test_session_initialized_is_a_standalone_record(tmp_path):
    """It still says what connected; it just does not join to anything.

    The value of 57360 is the statement that a client of this name and version
    connected at this time under this identity — not a correlation key.
    """
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def get_document_by_id(ctx: Context, bucket_name: str) -> str:
        """read a document"""
        return "doc"

    async with Client(mcp) as client:
        await client.call_tool("get_document_by_id", {"bucket_name": "b"})

    records = _records(audit)
    session = next(r for r in records if r["id"] == AuditEvent.SESSION_INITIALIZED.id)
    assert session["cid"]
    assert session["client_name"]
    assert session["protocol_version"]
    # Nothing else shares its cid.
    assert [r for r in records if r["cid"] == session["cid"]] == [session]


@pytest.mark.asyncio
async def test_guardrail_and_tool_call_records_share_one_cid(tmp_path):
    """Correlation must survive the path that travels through the state dict.

    A refusal is recorded by the gate and emitted by the middleware, so this is
    the case where a broken hand-off would show up as a mismatched id rather
    than as a missing record.
    """
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def delete_document_by_id(ctx: Context, bucket_name: str) -> str:
        """runs without confirmation because the client cannot elicit"""
        audit_state.record_confirmation(audit_state.CONFIRMATION_SKIPPED)
        return "deleted"

    async with Client(mcp) as client:
        await client.call_tool("delete_document_by_id", {"bucket_name": "b"})

    records = _records(audit)
    skipped = [r for r in records if r["id"] == AuditEvent.CONFIRMATION_SKIPPED.id]
    calls = [r for r in records if r["id"] == 61522]
    assert len(skipped) == 1
    assert len(calls) == 1
    assert skipped[0]["cid"] == calls[0]["cid"]


@pytest.mark.asyncio
async def test_scope_denial_record_carries_a_cid(tmp_path):
    """A refused call produces one record, and it is still correlatable."""
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def upsert_document_by_id(ctx: Context, bucket_name: str) -> str:
        """refused before doing anything"""
        audit_state.record_refusal(
            event_id=ScopeDeniedError.audit_event.id,
            event_name=ScopeDeniedError.audit_event.event_name,
            outcome=ScopeDeniedError.audit_outcome,
            reason=ScopeDeniedError.audit_reason,
            required_scope="couchbase-mcp:write",
        )
        raise ScopeDeniedError("missing scope")

    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool("upsert_document_by_id", {"bucket_name": "b"})

    records = _records(audit)
    denied = [r for r in records if r["id"] == AuditEvent.SCOPE_CHECK_DENIED.id]
    assert len(denied) == 1
    assert denied[0]["cid"]


@pytest.mark.asyncio
async def test_tool_code_can_read_the_requests_cid(tmp_path):
    """The hook a later phase needs to reach Couchbase Server audit.

    Propagating the ``cid`` to a SQL++ query as ``client_context_id`` requires
    tool code — running on a worker thread, for a sync tool — to see the same
    ``cid`` the middleware will stamp on the record.
    """
    audit = _build_logger(tmp_path)
    mcp = _server(audit)
    seen: dict[str, str | None] = {}

    @mcp.tool
    def get_document_by_id(ctx: Context, bucket_name: str) -> str:
        """read a document"""
        seen["cid"] = audit_state.get_cid()
        return "doc"

    async with Client(mcp) as client:
        await client.call_tool("get_document_by_id", {"bucket_name": "b"})

    records = _records(audit)
    call = next(r for r in records if r["id"] == 61490)
    assert seen["cid"] == call["cid"]


@pytest.mark.asyncio
async def test_correlation_survives_without_on_message(tmp_path):
    """The fallback: a hook invoked with no request state still correlates.

    ``on_message`` normally installs the state. A host that dispatches straight
    to ``on_call_tool`` must still produce joinable records rather than records
    with no ``cid`` at all.
    """
    audit = _build_logger(tmp_path)
    middleware = AuditMiddleware(
        transport="stdio", cb_userid="mcp_service", audit_logger=audit
    )

    @dataclass
    class _Message:
        name: str = "get_document_by_id"
        arguments: dict[str, str] = field(default_factory=lambda: {"bucket_name": "b"})

    @dataclass
    class _Context:
        message: _Message = field(default_factory=_Message)
        fastmcp_context: None = None

    async def _call_next(_context):
        return "doc"

    assert audit_state.current() is None
    await middleware.on_call_tool(_Context(), _call_next)
    # State is torn down again, so nothing leaks into the next request.
    assert audit_state.current() is None

    records = _records(audit)
    call = next(r for r in records if r["id"] == 61490)
    assert call["cid"]


@pytest.mark.asyncio
async def test_cid_is_per_request_over_a_real_http_transport(tmp_path):
    """stdio and in-memory share a code path; HTTP does not.

    Streamable HTTP assigns its own transport-level ``Mcp-Session-Id``, which
    must never leak into a record — the audit format has no session field.
    """
    audit = _build_logger(tmp_path)
    mcp = _server(audit, transport="streamable-http")

    @mcp.tool
    def get_document_by_id(ctx: Context, bucket_name: str) -> str:
        """read a document"""
        return "doc"

    # Port 0 so a busy port cannot make this test flaky; the kernel picks one
    # and we read it back off the bound socket.
    config = uvicorn.Config(
        mcp.http_app(transport="http"), host="127.0.0.1", port=0, log_level="error"
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        transport = StreamableHttpTransport(url=f"http://127.0.0.1:{port}/mcp/")
        async with Client(transport) as client:
            await client.call_tool("get_document_by_id", {"bucket_name": "b"})
            await client.call_tool("get_document_by_id", {"bucket_name": "b2"})
    finally:
        server.should_exit = True
        await serving

    records = _records(audit)
    calls = [r for r in records if r["id"] == 61490]
    assert len(calls) == 2
    assert calls[0]["cid"] != calls[1]["cid"]
    for record in records:
        assert "sid" not in record


# ---------------------------------------------------------------------------
# identity on the records that carry it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_record_carries_the_resolved_caller_identity(tmp_path):
    """``real_userid`` is the field the whole subsystem exists for.

    The cluster sees one service account for every caller, so this is the only
    place the actual caller is recorded. It was asserted on ``session
    initialized`` and ``token rejected`` and nowhere else — replacing the
    resolved identity with a hardcoded fake passed the entire suite, on every
    tool-call and refusal record.
    """
    audit = _build_logger(tmp_path)
    mcp = _server(audit)

    @mcp.tool
    def get_document_by_id(ctx: Context, bucket_name: str) -> str:
        """a plain read"""
        return "doc"

    @mcp.tool
    def run_sql_plus_plus_query(ctx: Context, bucket_name: str, query: str) -> str:
        """refused by the read-only gate"""
        audit_state.record_statement_class("write")
        audit_state.record_refusal(
            event_id=ReadOnlyWriteBlockedError.audit_event.id,
            event_name=ReadOnlyWriteBlockedError.audit_event.event_name,
            outcome=ReadOnlyWriteBlockedError.audit_outcome,
            reason=ReadOnlyWriteBlockedError.audit_reason,
        )
        raise ReadOnlyWriteBlockedError("not allowed in read-only mode")

    async with Client(mcp) as client:
        await client.call_tool("get_document_by_id", {"bucket_name": "b"})
        with contextlib.suppress(ToolError):
            await client.call_tool(
                "run_sql_plus_plus_query",
                {"bucket_name": "b", "query": "UPDATE airline SET x = 1"},
            )

    records = _records(audit)
    # stdio has no authenticated caller, so the honest answer is the OS process
    # owner in the ``local`` domain — resolved, not assumed.
    expected = resolve_real_userid("stdio")
    assert expected["domain"] == "local"

    carriers = [r for r in records if r["id"] in (61490, 57488)]
    assert len(carriers) == 2, [r["name"] for r in records]
    for record in carriers:
        assert record["real_userid"] == expected, record


@pytest.mark.asyncio
async def test_an_oauth_caller_is_named_on_the_tool_call_record(tmp_path):
    """The HTTP case: the record must name the token's subject, not the server."""
    audit = _build_logger(tmp_path)
    mcp = FastMCP("audit-test")
    mcp.add_middleware(
        AuditMiddleware(transport="http", cb_userid="mcp_service", audit_logger=audit)
    )

    @mcp.tool
    def get_document_by_id(ctx: Context, bucket_name: str) -> str:
        """a plain read"""
        return "doc"

    token = SimpleNamespace(
        claims={"sub": "agent-svc-7"}, client_id="agent-svc-7", scopes=["read"]
    )
    with patch("cb_mcp.audit.identity.get_access_token", return_value=token):
        async with Client(mcp) as client:
            await client.call_tool("get_document_by_id", {"bucket_name": "b"})

    calls = [r for r in _records(audit) if r["id"] == 61490]
    assert calls, "no tool-call record"
    assert calls[0]["real_userid"] == {"domain": "oauth", "user": "agent-svc-7"}
    # The cluster-side account is recorded separately and must not be mistaken
    # for the caller.
    assert calls[0]["cb_userid"] == "mcp_service"
