"""Resolution of ``real_userid`` — the access-decision identity.

This is the only trustworthy answer to "who did this", and the reason an MCP
audit log exists at all: the cluster only ever sees the single
``CB_USERNAME`` the server connects with, so Couchbase Server's own audit log
cannot tell two callers apart.

Three domains, per the PRD:

* ``oauth`` — Streamable HTTP with OAuth active. The user is the bearer token's
  subject.
* ``local`` — stdio. The user is the OS process owner.
* ``anonymous`` — HTTP without OAuth. There is no caller authentication, so
  there is no identity to record.

The ``domain`` is recorded faithfully rather than papered over, because two of
these cases are degraded and a reviewer needs to be able to filter them out
rather than trust them:

* ``anonymous`` collapses every caller together — exactly the problem auditing
  is meant to solve. :func:`warn_on_unauthenticated_http` reports this at
  startup.
* ``local`` inside a container resolves to the image's user (often ``root``),
  which looks like an identity but identifies nothing. Documented in
  ``AUDIT.md``.
"""

from __future__ import annotations

import getpass
import logging
import os

from fastmcp.server.dependencies import get_access_token

from ..utils.constants import LOGGER_NAMESPACE, STREAMABLE_HTTP_TRANSPORT

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.audit.identity")

DOMAIN_OAUTH = "oauth"
DOMAIN_LOCAL = "local"
DOMAIN_ANONYMOUS = "anonymous"

#: Recorded when the OS username cannot be determined at all.
UNKNOWN_USER = "unknown"


def _process_owner() -> str:
    """Best-effort OS process owner.

    ``getpass.getuser`` consults the environment before the password database
    and raises on a container with no matching passwd entry and no ``USER`` set,
    so both are guarded.
    """
    try:
        return getpass.getuser()
    except Exception:
        logger.debug("getpass.getuser() failed; falling back to uid", exc_info=True)
    try:
        return str(os.getuid())  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - Windows has no getuid
        return UNKNOWN_USER
    except Exception:  # pragma: no cover - defensive
        return UNKNOWN_USER


def _token_subject(token: object) -> str | None:
    """Extract the caller's subject from a FastMCP access token.

    Prefers the parsed ``subject``, then the raw ``sub`` claim, then the
    ``client_id``. A token that yields none of these is treated as having no
    usable identity rather than being reported under a misleading value.
    """
    subject = getattr(token, "subject", None)
    if isinstance(subject, str) and subject.strip():
        return subject.strip()

    claims = getattr(token, "claims", None)
    if isinstance(claims, dict):
        sub = claims.get("sub")
        if isinstance(sub, str) and sub.strip():
            return sub.strip()

    client_id = getattr(token, "client_id", None)
    if isinstance(client_id, str) and client_id.strip():
        return client_id.strip()

    return None


def resolve_real_userid(transport: str) -> dict[str, str]:
    """Return the ``{"domain": ..., "user": ...}`` pair for this request.

    Reads the request-scoped access token, which is ``None`` whenever no
    authenticated caller is present — stdio, or HTTP with OAuth disabled.
    """
    token = _current_token()

    if token is not None:
        subject = _token_subject(token)
        if subject is not None:
            return {"domain": DOMAIN_OAUTH, "user": subject}

    if transport == STREAMABLE_HTTP_TRANSPORT:
        return {"domain": DOMAIN_ANONYMOUS, "user": DOMAIN_ANONYMOUS}

    return {"domain": DOMAIN_LOCAL, "user": _process_owner()}


def _current_token() -> object | None:
    """Return this request's access token, or ``None`` outside a request.

    ``get_access_token`` raises when there is no request context at all — for
    example on a stdio server, or when auditing is exercised outside FastMCP —
    so every caller goes through this guard rather than repeating it.
    """
    try:
        return get_access_token()
    except Exception:
        logger.debug("No access token available for this request", exc_info=True)
        return None


def client_id_of_current_token() -> str | None:
    """Return the OAuth ``client_id`` for this request, when there is one."""
    token = _current_token()
    client_id = getattr(token, "client_id", None) if token is not None else None
    return client_id if isinstance(client_id, str) and client_id else None


def scopes_of_current_token() -> list[str] | None:
    """Return the scopes held by this request's token, when there is one."""
    token = _current_token()
    if token is None:
        return None
    scopes = getattr(token, "scopes", None)
    return sorted(scopes) if scopes else []


def warn_on_unauthenticated_http(transport: str, oauth_enabled: bool) -> None:
    """Warn when auditing is enabled on an HTTP transport without OAuth.

    In that configuration every record resolves to ``anonymous`` and the audit
    log cannot answer "who did this" — the operator should know at startup, not
    when an auditor asks.
    """
    if transport == STREAMABLE_HTTP_TRANSPORT and not oauth_enabled:
        logger.warning(
            "Audit logging is enabled on the %s transport without OAuth. "
            "Every audit record will report real_userid "
            '{"domain": "anonymous", "user": "anonymous"} because callers are '
            "not authenticated. Configure the CB_MCP_OAUTH_JWT_* settings to "
            "record the actual caller identity.",
            transport,
        )


__all__ = [
    "DOMAIN_ANONYMOUS",
    "DOMAIN_LOCAL",
    "DOMAIN_OAUTH",
    "UNKNOWN_USER",
    "client_id_of_current_token",
    "resolve_real_userid",
    "scopes_of_current_token",
    "warn_on_unauthenticated_http",
]
