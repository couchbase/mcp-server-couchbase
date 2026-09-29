"""The audit middleware.

Sits in FastMCP's message pipeline and produces one record per tool call, plus
the session and guardrail records. It is deliberately *not* the only audit hook:
a rejected bearer token never becomes a JSON-RPC message, so it is caught in
the JWT verifier, and server lifecycle records come from the lifespan. What
middleware alone cannot see is *why* a call was refused — FastMCP wraps a
tool's exception in ``ToolError`` — so refusals are recorded by the gate that
made the decision, into :mod:`cb_mcp.audit.state`, and read back here.

Correlation is minted here and nowhere else. ``on_message`` is the outermost
middleware hook and runs for every MCP request — ``initialize``, ``tools/call``,
the listings — so it is the one place that mints the per-request ``cid``. Every
other hook, and every guardrail, only *reads* it out of
:mod:`cb_mcp.audit.state`.

Doing it here rather than in the authentication layer is deliberate. There is
no "token accepted" event, so on the success path the auth layer emits no
record for a shared ``cid`` to join to, and a *rejected* token is refused
before any MCP message exists, which makes its record a singleton whatever id
it carries. Minting in middleware therefore loses nothing, and it means a host
with a completely different authentication stack — the managed Capella runtime
— inherits the whole correlation model without writing any code.

A request is the only correlation scope. No session identifier is recorded:
MCP is moving to a stateless model in which a server must not treat connection
or process identity as a proxy for session continuity, so an id derived from it
would group unrelated interleaved conversations together.

Emission rules, following the PRD:

* A call refused at a gate emits **only** the core authorization or guardrail
  event. The tool never ran, so there is no tool-call event; the ``reason``
  vocabulary for tool calls covers execution failures, not policy decisions.
* A call that ran emits a tool-call event with ``success`` or ``error``.
* A confirmation that was silently skipped emits its own guardrail record
  *and* is noted on the tool-call record, because the tool did run. Both
  records share the correlation id, so they join.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastmcp.exceptions import NotFoundError
from fastmcp.server.middleware import Middleware

from ..utils.constants import LOGGER_NAMESPACE
from . import state
from .catalog import DEFAULT_SERVICE_PACKAGE, AuditEvent
from .classification import (
    STATEMENT_CLASSIFIED_TOOL,
    classify_tool,
    is_classified,
    required_scope_for,
    resolve_tool_call_event,
)
from .emitter import AuditLogger, get_audit_logger
from .identity import (
    client_id_of_current_token,
    resolve_real_userid,
    scopes_of_current_token,
)
from .record import (
    OUTCOME_ERROR,
    OUTCOME_SUCCESS,
    REASON_EXECUTION_ERROR,
)

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.audit.middleware")

#: Argument names that together identify the keyspace a tool acted on. Every
#: keyspace-scoped tool in this server uses exactly these names.
_KEYSPACE_ARGS = ("bucket_name", "scope_name", "collection_name")

#: Event ids that describe a policy refusal rather than an invocation.
_REFUSAL_EVENTS: dict[int, AuditEvent] = {
    event.id: event
    for event in (
        AuditEvent.SCOPE_CHECK_DENIED,
        AuditEvent.WRITE_BLOCKED_READ_ONLY,
        AuditEvent.CONFIRMATION_DECLINED,
    )
}


def extract_keyspace(arguments: dict[str, Any] | None) -> str | None:
    """Build the dotted ``bucket.scope.collection`` keyspace from tool arguments.

    Returns the longest unbroken prefix that is present, so a bucket-only tool
    yields ``"travel-sample"`` and a document tool yields
    ``"travel-sample.inventory.airline"``. Returns ``None`` for tools that do
    not address a keyspace at all, such as the cluster-level ones.
    """
    if not arguments:
        return None
    parts: list[str] = []
    for name in _KEYSPACE_ARGS:
        value = arguments.get(name)
        if not isinstance(value, str) or not value:
            break
        parts.append(value)
    return ".".join(parts) if parts else None


@contextmanager
def _request_state(context: Any) -> Iterator[dict[str, Any]]:
    """Yield this request's audit state, installing it if it is missing.

    ``on_message`` normally installs the state, so the inner hooks find it
    already there and reuse it — that is what keeps one ``cid`` across a
    request. The fallback covers a hook invoked without ``on_message`` having
    run: correlation degrades to per-hook rather than disappearing.
    """
    existing = state.current()
    if existing is not None:
        yield existing
        return
    token = state.install(cid=str(uuid.uuid4()))
    try:
        installed = state.current()
        yield installed if installed is not None else {}
    finally:
        state.reset(token)


class AuditMiddleware(Middleware):
    """Emits session and tool-call audit records."""

    def __init__(
        self,
        *,
        transport: str,
        cb_userid: str | None = None,
        service_package: str = DEFAULT_SERVICE_PACKAGE,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        """
        Args:
            transport: The resolved transport name, used to decide whether an
                unauthenticated caller is ``anonymous`` (HTTP) or the local
                process owner (stdio).
            service_package: Which Tier-2 block this server's tool calls are
                booked against, and which classification table names them.
                Supplied from ``ServerSpec.audit_package`` so two servers that
                share a tool name cannot share an event id.
            cb_userid: The Couchbase user the server connects to the cluster
                with. Recorded so a reviewer can see which cluster identity the
                MCP-layer caller was mapped onto — the collapse this audit log
                exists to undo.
            audit_logger: Injected in tests; defaults to the process logger.
        """
        self._transport = transport
        self._cb_userid = cb_userid
        self._service_package = service_package
        self._injected_logger = audit_logger

    @property
    def _audit(self) -> AuditLogger:
        return self._injected_logger or get_audit_logger()

    # -- correlation ------------------------------------------------------

    async def on_message(self, context, call_next):  # type: ignore[no-untyped-def]
        """Mint this request's correlation id, for every hook below to read.

        Runs for every MCP *request*; notifications reach neither this hook nor
        ``on_notification`` in FastMCP 3.4.6, so nothing is minted for them.
        """
        audit = self._audit
        if not audit.active:
            return await call_next(context)

        token = state.install(cid=str(uuid.uuid4()))
        try:
            return await call_next(context)
        finally:
            state.reset(token)

    # -- session ----------------------------------------------------------

    async def on_initialize(self, context, call_next):  # type: ignore[no-untyped-def]
        """Record the MCP initialize handshake: what connected.

        A standalone record: it carries this request's ``cid`` like any other,
        but nothing later joins to it, because no session identifier is
        recorded. Its value is the statement that a client of this name and
        version connected at this time, under this identity.
        """
        result = await call_next(context)
        audit = self._audit
        if not audit.active:
            return result
        try:
            params = getattr(context.message, "params", None)
            client_info = getattr(params, "clientInfo", None)
            with _request_state(context) as request_state:
                audit.emit_event(
                    AuditEvent.SESSION_INITIALIZED,
                    outcome=OUTCOME_SUCCESS,
                    real_userid=resolve_real_userid(self._transport),
                    cid=request_state.get("cid"),
                    protocol_version=getattr(params, "protocolVersion", None),
                    client_name=getattr(client_info, "name", None),
                    client_version=getattr(client_info, "version", None),
                )
        except Exception:  # auditing must never break a session
            logger.exception("Failed to emit the session-initialized audit record")
        return result

    # -- tool calls -------------------------------------------------------

    async def on_call_tool(self, context, call_next):  # type: ignore[no-untyped-def]
        """Record one tool call, or the gate that refused it."""
        audit = self._audit
        if not audit.active:
            return await call_next(context)

        tool_name = getattr(context.message, "name", None) or "unknown"
        arguments = getattr(context.message, "arguments", None)
        real_userid = resolve_real_userid(self._transport)

        with _request_state(context) as request_state:
            cid = request_state.get("cid")
            try:
                result = await call_next(context)
            except Exception as exc:
                self._emit_outcome(
                    audit,
                    tool_name=tool_name,
                    arguments=arguments,
                    cid=cid,
                    real_userid=real_userid,
                    failure=exc,
                )
                raise
            self._emit_outcome(
                audit,
                tool_name=tool_name,
                arguments=arguments,
                cid=cid,
                real_userid=real_userid,
                failure=None,
                result=result,
            )
            return result

    # -- emission ---------------------------------------------------------

    def _emit_outcome(
        self,
        audit: AuditLogger,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None,
        cid: str | None,
        real_userid: dict[str, str],
        failure: BaseException | None,
        result: Any = None,
    ) -> None:
        """Emit whichever records this call earned. Never raises."""
        try:
            if isinstance(failure, NotFoundError):
                # The tool does not exist as far as this server is concerned —
                # either the name is wrong, or the tool was withheld at
                # registration by read-only mode or the disabled set. Nothing
                # executed, so emitting a tool-call record would state an
                # ``execution_error`` that never happened, and withheld tools
                # deliberately produce no audit event: a client is never told
                # they exist, so there is no refusal to record. The enforced
                # tool surface is captured once by the server-configuration
                # event at startup instead.
                logger.debug(
                    "No audit record for unknown or withheld tool %r", tool_name
                )
                return

            confirmation = state.get_confirmation()
            if confirmation == state.CONFIRMATION_SKIPPED:
                # Not ``blocked``: the tool *ran*, unconfirmed, which is what
                # the catalogue entry describes. Recording it as blocked would
                # make a reviewer counting ``outcome=blocked`` read unconfirmed
                # destructive executions as prevented ones — the exact opposite
                # of what happened. The ``reason`` carries the disposition.
                audit.emit_event(
                    AuditEvent.CONFIRMATION_SKIPPED,
                    outcome=OUTCOME_SUCCESS,
                    real_userid=real_userid,
                    cid=cid,
                    reason="confirmation_unsupported",
                    tool_name=tool_name,
                    confirmation="skipped",
                )

            refusal = state.get_refusal()
            if refusal is not None:
                self._emit_refusal(
                    audit,
                    refusal=refusal,
                    tool_name=tool_name,
                    cid=cid,
                    real_userid=real_userid,
                )
                return

            self._emit_tool_call(
                audit,
                tool_name=tool_name,
                arguments=arguments,
                cid=cid,
                real_userid=real_userid,
                confirmation=confirmation,
                failure=failure,
                result=result,
            )
        except Exception:  # auditing must never break a call
            logger.exception("Failed to emit an audit record for tool %r", tool_name)

    def _emit_refusal(
        self,
        audit: AuditLogger,
        *,
        refusal: dict[str, Any],
        tool_name: str,
        cid: str | None,
        real_userid: dict[str, str],
    ) -> None:
        """Emit the core authorization or guardrail record for a refused call."""
        event = _REFUSAL_EVENTS.get(int(refusal["event_id"]))
        if event is None:  # pragma: no cover - guarded by the state contract
            logger.warning(
                "Ignoring audit refusal for unknown event id %r",
                refusal.get("event_id"),
            )
            return

        payload: dict[str, Any] = {"tool_name": tool_name}
        for key, value in refusal.items():
            if key not in ("event_id", "event_name", "outcome", "reason"):
                payload[key] = value

        if event is AuditEvent.SCOPE_CHECK_DENIED:
            payload.setdefault("client_id", client_id_of_current_token())
            payload.setdefault("scopes", scopes_of_current_token())
            # One shape for this field across every event that carries it: the
            # short class, as on tool-call records. ``setdefault`` leaves the
            # SQL++ statement gate's explicit value alone, since that gate
            # classifies per statement rather than per tool.
            _, operation_class = classify_tool(tool_name, package=self._service_package)
            payload.setdefault("required_scope", required_scope_for(operation_class))

        audit.emit_event(
            event,
            outcome=str(refusal["outcome"]),
            real_userid=real_userid,
            cid=cid,
            reason=str(refusal["reason"]),
            **payload,
        )

    def _emit_tool_call(
        self,
        audit: AuditLogger,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None,
        cid: str | None,
        real_userid: dict[str, str],
        confirmation: str | None,
        failure: BaseException | None,
        result: Any,
    ) -> None:
        """Emit the Tier-2 record for a call that actually reached the tool."""
        override = (
            state.get_statement_class()
            if tool_name == STATEMENT_CLASSIFIED_TOOL
            else None
        )
        event = resolve_tool_call_event(
            tool_name,
            package=self._service_package,
            operation_class_override=override,
        )

        outcome = OUTCOME_SUCCESS
        reason: str | None = None
        if failure is not None:
            outcome = OUTCOME_ERROR
            reason = REASON_EXECUTION_ERROR
        elif getattr(result, "is_error", False):
            # A tool that reports failure through the result rather than by
            # raising is still a failed operation as far as an auditor cares.
            outcome = OUTCOME_ERROR
            reason = REASON_EXECUTION_ERROR

        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "required_scope": required_scope_for(event.operation_class),
            "ks": extract_keyspace(arguments),
            "cb_userid": self._cb_userid,
            "confirmation": confirmation,
        }
        if not is_classified(tool_name, self._service_package):
            # Fail-closed classification: recorded against a write id so it is
            # never filtered, and flagged so the placeholder category is never
            # mistaken for a real one.
            payload["unclassified"] = True
        if audit.config.tool_args and arguments:
            payload["args"] = arguments

        audit.emit_tool_call(
            event,
            outcome=outcome,
            real_userid=real_userid,
            cid=cid,
            reason=reason,
            **payload,
        )


__all__ = ["AuditMiddleware", "extract_keyspace"]
