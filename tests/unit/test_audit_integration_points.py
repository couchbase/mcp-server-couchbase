"""Tests for the audit hooks added to existing gates.

Auditing required narrowing three exception types that already existed. Every
new class subclasses the type the call site raised before, so existing callers
and tests are unaffected — these tests assert that backwards compatibility
explicitly, because it is the whole reason the refactor was safe.

They also assert the other half of the contract: each gate records a structured
refusal into the per-request audit state *before* raising, since FastMCP wraps
the exception and the middleware could not otherwise tell a scope denial from a
read-only block from a genuine error.

Coverage map:
- exception subclassing preserves PermissionError / ValueError contracts
- scope check records a refusal and raises ScopeDeniedError
- confirmation decline records a refusal and raises ConfirmationDeclinedError
- confirmation skip records the skipped disposition
- confirmation accept records the accepted disposition
- read-only SQL++ block records a refusal and raises ReadOnlyWriteBlockedError
- SQL++ statement class is recorded for audit when auditing is active
- SQL++ classification is not computed when auditing is off (no added cost)
- identity resolution across the three domains
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp import Context

from cb_mcp.audit import state as audit_state
from cb_mcp.audit.exceptions import (
    ConfirmationDeclinedError,
    ReadOnlyWriteBlockedError,
    ScopeDeniedError,
    StatementScopeDeniedError,
)
from cb_mcp.audit.identity import (
    DOMAIN_ANONYMOUS,
    DOMAIN_LOCAL,
    DOMAIN_OAUTH,
    resolve_real_userid,
    warn_on_unauthenticated_http,
)
from cb_mcp.tools.query import _query_correlation_options, run_sql_plus_plus_query
from cb_mcp.utils.constants import SCOPE_READ, SCOPE_WRITE
from cb_mcp.utils.elicitation import ConfirmationResult, wrap_with_confirmation
from cb_mcp.utils.scope_enforcement import wrap_with_scope_check

# ---------------------------------------------------------------------------
# backwards compatibility of the narrowed exception types
# ---------------------------------------------------------------------------


def test_exceptions_preserve_their_original_base_types():
    """Existing ``pytest.raises`` assertions must keep passing unchanged."""
    assert issubclass(ScopeDeniedError, PermissionError)
    assert issubclass(StatementScopeDeniedError, PermissionError)
    assert issubclass(ConfirmationDeclinedError, PermissionError)
    assert issubclass(ReadOnlyWriteBlockedError, ValueError)


@pytest.mark.parametrize(
    "exception_class",
    [
        ScopeDeniedError,
        StatementScopeDeniedError,
        ConfirmationDeclinedError,
        ReadOnlyWriteBlockedError,
    ],
)
def test_exceptions_carry_their_audit_classification(exception_class):
    assert exception_class.audit_event.id
    assert exception_class.audit_outcome in ("denied", "blocked")
    assert exception_class.audit_reason


# ---------------------------------------------------------------------------
# scope enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_check_records_a_refusal_and_raises_scope_denied():
    async def tool(ctx=None):
        return "should not run"

    tool.__name__ = "upsert_document_by_id"
    wrapped = wrap_with_scope_check(tool, {SCOPE_WRITE})
    token = audit_state.install()
    try:
        with (
            patch(
                "cb_mcp.utils.scope_enforcement.get_access_token",
                return_value=SimpleNamespace(scopes=[SCOPE_READ]),
            ),
            pytest.raises(ScopeDeniedError),
        ):
            await wrapped()
        refusal = audit_state.get_refusal()
        assert refusal is not None
        assert refusal["event_id"] == 57377
        assert refusal["outcome"] == "denied"
        assert refusal["reason"] == "missing_scope"
        assert refusal["required_scope"] == [SCOPE_WRITE]
    finally:
        audit_state.reset(token)


@pytest.mark.asyncio
async def test_scope_check_records_nothing_when_the_token_is_sufficient():
    async def tool(ctx=None):
        return "ran"

    tool.__name__ = "upsert_document_by_id"
    wrapped = wrap_with_scope_check(tool, {SCOPE_WRITE})
    token = audit_state.install()
    try:
        with patch(
            "cb_mcp.utils.scope_enforcement.get_access_token",
            return_value=SimpleNamespace(scopes=[SCOPE_WRITE]),
        ):
            assert await wrapped() == "ran"
        assert audit_state.get_refusal() is None
    finally:
        audit_state.reset(token)


# ---------------------------------------------------------------------------
# confirmation
# ---------------------------------------------------------------------------


def _ctx_with_elicitation(
    *, supported: bool, action: str = "accept", confirm: bool = True
) -> Context:
    async def elicit(message, schema):
        return SimpleNamespace(action=action, data=ConfirmationResult(confirm=confirm))

    session = SimpleNamespace(check_client_capability=lambda _caps: supported)
    return SimpleNamespace(  # type: ignore[return-value]
        request_context=SimpleNamespace(session=session), elicit=elicit
    )


@pytest.mark.asyncio
async def test_declined_confirmation_records_a_refusal():
    async def tool(ctx=None):
        return "should not run"

    tool.__name__ = "delete_document_by_id"
    wrapped = wrap_with_confirmation(tool)
    token = audit_state.install()
    try:
        with pytest.raises(ConfirmationDeclinedError):
            await wrapped(ctx=_ctx_with_elicitation(supported=True, action="decline"))
        refusal = audit_state.get_refusal()
        assert refusal["event_id"] == 57490
        assert refusal["outcome"] == "blocked"
        assert refusal["reason"] == "confirmation_declined"
        assert audit_state.get_confirmation() == "declined"
    finally:
        audit_state.reset(token)


@pytest.mark.asyncio
async def test_skipped_confirmation_is_recorded_and_the_tool_still_runs():
    """The pre-audit behaviour left this bypass invisible above DEBUG."""

    async def tool(ctx=None):
        return "ran without confirmation"

    tool.__name__ = "delete_document_by_id"
    wrapped = wrap_with_confirmation(tool)
    token = audit_state.install()
    try:
        result = await wrapped(ctx=_ctx_with_elicitation(supported=False))
        assert result == "ran without confirmation"
        assert audit_state.get_confirmation() == "skipped"
        # Not a refusal: the call proceeded.
        assert audit_state.get_refusal() is None
    finally:
        audit_state.reset(token)


@pytest.mark.asyncio
async def test_accepted_confirmation_is_recorded():
    async def tool(ctx=None):
        return "ran"

    tool.__name__ = "delete_document_by_id"
    wrapped = wrap_with_confirmation(tool)
    token = audit_state.install()
    try:
        assert await wrapped(ctx=_ctx_with_elicitation(supported=True)) == "ran"
        assert audit_state.get_confirmation() == "accepted"
    finally:
        audit_state.reset(token)


# ---------------------------------------------------------------------------
# SQL++ read-only guard and statement classification
# ---------------------------------------------------------------------------


def _query_ctx(*, read_only_mode: bool):
    scope = SimpleNamespace(query=lambda *a, **k: iter(()))
    bucket = SimpleNamespace(scope=lambda _name: scope)
    cluster = SimpleNamespace(bucket=lambda _name: bucket)
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=SimpleNamespace(
                cluster_provider=SimpleNamespace(get_cluster=lambda _ctx: cluster),
                read_only_mode=read_only_mode,
            )
        )
    )
    return ctx


def test_read_only_block_records_a_refusal_and_raises_a_value_error():
    ctx = _query_ctx(read_only_mode=True)
    token = audit_state.install()
    try:
        # Still a ValueError, so pre-existing callers are unaffected.
        with pytest.raises(ValueError, match="not allowed in read-only mode"):
            run_sql_plus_plus_query(ctx, "b", "s", "UPDATE users SET age = 25")
        refusal = audit_state.get_refusal()
        assert refusal["event_id"] == 57488
        assert refusal["outcome"] == "blocked"
        assert refusal["reason"] == "read_only_mode"
        assert refusal["statement_kind"] == "data"
    finally:
        audit_state.reset(token)


def test_statement_class_is_recorded_when_auditing_is_active():
    """An *allowed* DML must still be classified, or it would be filed as a read."""
    ctx = _query_ctx(read_only_mode=False)
    token = audit_state.install()
    try:
        with patch(
            "cb_mcp.tools.query.get_audit_logger",
            return_value=SimpleNamespace(active=True),
        ):
            run_sql_plus_plus_query(ctx, "b", "s", "UPDATE users SET age = 25")
        assert audit_state.get_statement_class() == "write"
    finally:
        audit_state.reset(token)


def test_select_is_classified_read_when_auditing_is_active():
    ctx = _query_ctx(read_only_mode=False)
    token = audit_state.install()
    try:
        with patch(
            "cb_mcp.tools.query.get_audit_logger",
            return_value=SimpleNamespace(active=True),
        ):
            run_sql_plus_plus_query(ctx, "b", "s", "SELECT * FROM users")
        assert audit_state.get_statement_class() == "read"
    finally:
        audit_state.reset(token)


def test_explain_is_classified_read_without_parsing():
    ctx = _query_ctx(read_only_mode=True)
    token = audit_state.install()
    try:
        with patch(
            "cb_mcp.tools.query.get_audit_logger",
            return_value=SimpleNamespace(active=True),
        ):
            run_sql_plus_plus_query(ctx, "b", "s", "EXPLAIN SELECT * FROM users")
        assert audit_state.get_statement_class() == "read"
    finally:
        audit_state.reset(token)


def test_no_statement_parse_when_auditing_is_off_and_writes_are_allowed():
    """Auditing off must not add cost to the busiest tool in the server."""
    ctx = _query_ctx(read_only_mode=False)
    token = audit_state.install()
    try:
        with patch("cb_mcp.tools.query.parse_sqlpp") as parse:
            with patch(
                "cb_mcp.tools.query.get_audit_logger",
                return_value=SimpleNamespace(active=False),
            ):
                run_sql_plus_plus_query(ctx, "b", "s", "UPDATE users SET age = 25")
            parse.assert_not_called()
        assert audit_state.get_statement_class() is None
    finally:
        audit_state.reset(token)


def test_unparseable_statement_still_runs_when_writes_are_allowed_and_auditing_on():
    """Enabling auditing must not fail a call the server would otherwise run.

    lark_sqlpp is a partial grammar: valid statements it cannot parse (MERGE, a
    leading block comment, newer syntax) raise from ``parse_sqlpp``. With writes
    allowed the parse is only for audit classification, so a parse failure must
    degrade to the fail-closed 'write' class and let the query execute — not
    surface as a tool-call failure that appears only when auditing is on.
    """
    ctx = _query_ctx(read_only_mode=False)
    token = audit_state.install()
    try:
        with (
            patch(
                "cb_mcp.tools.query.parse_sqlpp",
                side_effect=Exception("unparseable by the partial grammar"),
            ),
            patch(
                "cb_mcp.tools.query.get_audit_logger",
                return_value=SimpleNamespace(active=True),
            ),
        ):
            result = run_sql_plus_plus_query(
                ctx, "b", "s", "MERGE INTO a USING b ON a.id = b.id"
            )
        # Executed rather than raised, and booked against the never-filtered
        # query write id so a possible mutation is never dropped from the log.
        assert result == []
        assert audit_state.get_statement_class() == "write"
    finally:
        audit_state.reset(token)


def test_unparseable_statement_still_fails_safe_when_writes_are_blocked():
    """The security write guard must keep failing closed on an unparseable
    statement: without a definitive read/write verdict it cannot prove the
    statement is a read, so it must refuse rather than execute. This is the
    pre-audit behaviour and must be preserved exactly."""
    ctx = _query_ctx(read_only_mode=True)
    token = audit_state.install()
    try:
        with (
            patch(
                "cb_mcp.tools.query.parse_sqlpp",
                side_effect=Exception("unparseable by the partial grammar"),
            ),
            patch(
                "cb_mcp.tools.query.get_audit_logger",
                return_value=SimpleNamespace(active=True),
            ),
            pytest.raises(Exception, match="unparseable"),
        ):
            run_sql_plus_plus_query(
                ctx, "b", "s", "MERGE INTO a USING b ON a.id = b.id"
            )
    finally:
        audit_state.reset(token)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_stdio_resolves_to_the_local_process_owner():
    with patch("cb_mcp.audit.identity.get_access_token", return_value=None):
        identity = resolve_real_userid("stdio")
    assert identity["domain"] == DOMAIN_LOCAL
    assert identity["user"]


def test_http_without_a_token_is_anonymous():
    with patch("cb_mcp.audit.identity.get_access_token", return_value=None):
        identity = resolve_real_userid("http")
    assert identity == {"domain": DOMAIN_ANONYMOUS, "user": DOMAIN_ANONYMOUS}


def test_oauth_prefers_the_token_subject():
    token = SimpleNamespace(
        subject="agent-svc-7",
        claims={"sub": "ignored"},
        client_id="client-1",
        scopes=[],
    )
    with patch("cb_mcp.audit.identity.get_access_token", return_value=token):
        identity = resolve_real_userid("http")
    assert identity == {"domain": DOMAIN_OAUTH, "user": "agent-svc-7"}


def test_oauth_falls_back_to_the_sub_claim_then_client_id():
    sub_only = SimpleNamespace(
        subject=None, claims={"sub": "from-claim"}, client_id="client-1", scopes=[]
    )
    with patch("cb_mcp.audit.identity.get_access_token", return_value=sub_only):
        assert resolve_real_userid("http")["user"] == "from-claim"

    client_only = SimpleNamespace(
        subject=None, claims={}, client_id="client-1", scopes=[]
    )
    with patch("cb_mcp.audit.identity.get_access_token", return_value=client_only):
        assert resolve_real_userid("http")["user"] == "client-1"


def test_token_with_no_usable_identity_is_not_reported_as_oauth():
    """Recording an empty subject under the oauth domain would be misleading."""
    empty = SimpleNamespace(subject=None, claims={}, client_id=None, scopes=[])
    with patch("cb_mcp.audit.identity.get_access_token", return_value=empty):
        assert resolve_real_userid("http")["domain"] == DOMAIN_ANONYMOUS


def test_unauthenticated_http_warns_at_startup(caplog):
    with caplog.at_level(logging.WARNING):
        warn_on_unauthenticated_http("http", oauth_enabled=False)
    assert "anonymous" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        warn_on_unauthenticated_http("http", oauth_enabled=True)
        warn_on_unauthenticated_http("stdio", oauth_enabled=False)
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# SQL++ correlation id propagation
# ---------------------------------------------------------------------------


def test_query_carries_no_correlation_id_when_auditing_is_off():
    """An unaudited query must be byte-for-byte the request it was before.

    The options dict is spread into the SDK call, so an empty dict means the
    call site is unchanged from the pre-audit behaviour.
    """
    assert audit_state.current() is None
    assert _query_correlation_options() == {}


def test_query_carries_the_requests_correlation_id_when_auditing_is_on():
    """The id we stamp on the audit record is the one Server will see.

    Couchbase Server's SQL++ audit record carries ``clientContextId``, taken
    from this parameter. Sending the same ``cid`` is what makes an MCP record
    and a Server record joinable by equality rather than by timestamp.
    """
    token = audit_state.install(cid="cid-abc-123")
    try:
        assert _query_correlation_options() == {"client_context_id": "cid-abc-123"}
    finally:
        audit_state.reset(token)


def test_query_correlation_is_omitted_when_state_carries_no_cid():
    """State can exist without a cid — a hook invoked outside on_message."""
    token = audit_state.install()
    try:
        assert _query_correlation_options() == {}
    finally:
        audit_state.reset(token)
