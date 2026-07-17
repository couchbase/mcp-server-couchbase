"""
OAuth authentication for the Couchbase MCP Server.

The MCP server acts as a pure OAuth 2.0 resource server: it validates bearer
JWTs against a JWKS published by the customer's identity provider, and
(optionally) advertises itself via RFC 9728 Protected Resource Metadata so
PRM-aware MCP clients can discover the authorization server and perform
Dynamic Client Registration directly against that IdP.

This module deliberately does NOT proxy DCR, refresh tokens, or any other
authorization-server responsibility. Customers who need DCR for IdPs that
don't support it should run a client-side OAuth proxy (e.g. mcp-remote).

Provider-agnostic by construction: any OAuth 2.1 / OIDC provider that
publishes a JWKS works (Auth0, Stytch, Okta, Keycloak, Azure AD, etc.).

Activation is controlled entirely from the CLI / env-var layer; this module
just turns validated configuration into the appropriate FastMCP auth
provider.
"""

import logging
from typing import ClassVar

from fastmcp.server.auth import AuthProvider, RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl

from .utils.constants import (
    DEFAULT_OAUTH_ALGORITHM,
    MCP_SERVER_NAME,
    SCOPE_READ,
    SCOPE_WRITE,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.auth")


class CouchbaseJWTVerifier(JWTVerifier):
    """JWTVerifier that maps IdP-specific scope strings onto our canonical scopes.

    Some IdPs cannot emit our canonical colon-form scope identifiers verbatim.
    Two well-known cases are handled here:

      - AWS Cognito Resource Servers prepend the RS identifier with ``/``, so
        a scope named ``read`` under RS ``couchbase-mcp`` shows up in tokens
        as ``couchbase-mcp/read``. When RFC 8707 resource-binding is in play,
        the RS identifier must equal the resource URL, producing
        ``<base_url>/mcp/read`` instead.
      - Some operators prefer a dashed form (``couchbase-mcp-read``) when the
        IdP UI disallows ``:`` or ``/`` in scope names.

    Per-instance ``scope_aliases`` (provided by the operator via CLI / env)
    take precedence over the built-in aliases, so a customer can declare the
    exact strings their IdP issues without code changes.

    Any scope not in either map passes through unchanged, so canonical-form
    tokens from Auth0 / Keycloak / Stytch are unaffected.
    """

    # Built-in aliases for the most common ``couchbase-mcp``-prefixed
    # variants. Operator-supplied entries via ``scope_aliases`` override
    # these for the same key.
    _BUILTIN_ALIASES: ClassVar[dict[str, str]] = {
        "couchbase-mcp/read": SCOPE_READ,
        "couchbase-mcp-read": SCOPE_READ,
        "couchbase-mcp/write": SCOPE_WRITE,
        "couchbase-mcp-write": SCOPE_WRITE,
    }

    def __init__(
        self,
        *,
        scope_aliases: dict[str, str] | None = None,
        **jwt_verifier_kwargs,
    ) -> None:
        """
        Args:
            scope_aliases: Optional per-instance map from IdP-issued scope
                strings to canonical scopes. Used when the customer's IdP
                cannot emit the canonical form (e.g. Cognito Resource Server
                prefixing). Operator entries take precedence over the
                built-in aliases.
            **jwt_verifier_kwargs: Forwarded to ``JWTVerifier.__init__``
                (``jwks_uri``, ``issuer``, ``audience``, ``algorithm``, etc.).
        """
        super().__init__(**jwt_verifier_kwargs)
        # Operator-provided entries shadow built-ins on the same key.
        self._scope_aliases: dict[str, str] = {
            **self._BUILTIN_ALIASES,
            **(scope_aliases or {}),
        }

    def _extract_scopes(self, claims: dict) -> list[str]:
        """Extract scopes and apply alias normalization.

        FastMCP's base reads from ``scope`` (space-separated) or ``scp``
        (array). We override post-extraction so that downstream per-tool
        enforcement sees a single canonical scope set regardless of which
        IdP issued the token.
        """
        raw = super()._extract_scopes(claims)
        return [self._scope_aliases.get(s, s) for s in raw]


def build_oauth(
    *,
    jwks_uri: str,
    issuer: str,
    audience: str,
    algorithm: str = DEFAULT_OAUTH_ALGORITHM,
    base_url: str | None = None,
    scope_read: str | None = None,
    scope_write: str | None = None,
) -> AuthProvider:
    """Build the FastMCP ``AuthProvider`` for the configured OAuth setup.

    Returns a bare ``CouchbaseJWTVerifier`` when ``base_url`` is omitted —
    the server validates tokens but does not publish protected-resource
    metadata. Pass ``base_url`` to wrap the verifier in a
    ``RemoteAuthProvider`` that also serves RFC 9728 metadata at
    ``<base_url>/.well-known/oauth-protected-resource/mcp``, advertising the
    IdP (derived from ``issuer``) and the two supported scopes so PRM-aware
    clients can discover the authorization server.

    Args:
        jwks_uri: JWKS endpoint of the upstream IdP. Used to fetch and rotate
            the signing keys that validate bearer JWTs.
        issuer: Expected ``iss`` claim. Also published as the authorization
            server in PRM when ``base_url`` is set.
        audience: Expected ``aud`` claim. Tokens not bound to this audience
            are rejected.
        algorithm: JWT signing algorithm to accept (must be in
            ``ALLOWED_OAUTH_ALGORITHMS``; validated by the CLI layer).
        base_url: Public base URL of this MCP server. When provided, enables
            PRM publication via ``RemoteAuthProvider``.
        scope_read: Override the OAuth scope string the server treats as
            ``read`` access. Defaults to ``SCOPE_READ`` (``couchbase-mcp:read``).
            Use this when the customer's IdP cannot emit the canonical form
            (e.g. AWS Cognito Resource Server prefixing). The configured
            value is advertised in PRM and accepted in token claims; it's
            normalized to ``SCOPE_READ`` internally so per-tool enforcement
            keeps its single canonical view.
        scope_write: Override for write access. Defaults to ``SCOPE_WRITE``
            (``couchbase-mcp:write``). Same semantics as ``scope_read``.

    Returns:
        ``AuthProvider`` suitable for passing directly to
        ``FastMCP(auth=...)``.

    Caller contract: the CLI layer is responsible for refusing partial
    configuration before calling this function. No defensive None-checks
    here — passing empty required arguments is a programmer error.
    """
    # Resolve the operator's effective scope names. These show up in PRM and
    # in the alias map; downstream per-tool enforcement always uses the
    # canonical SCOPE_READ / SCOPE_WRITE constants.
    effective_read = scope_read or SCOPE_READ
    effective_write = scope_write or SCOPE_WRITE

    # Build per-instance aliases only for non-canonical operator overrides;
    # if the operator passed the canonical value (or nothing), nothing extra
    # is needed beyond the built-in normalizations.
    aliases: dict[str, str] = {}
    if effective_read != SCOPE_READ:
        aliases[effective_read] = SCOPE_READ
    if effective_write != SCOPE_WRITE:
        aliases[effective_write] = SCOPE_WRITE

    verifier = CouchbaseJWTVerifier(
        jwks_uri=jwks_uri,
        issuer=issuer,
        audience=audience,
        algorithm=algorithm,
        scope_aliases=aliases or None,
        # required_scopes left None: per-tool enforcement happens inside
        # wrap_with_scope_check (see utils/scope_enforcement.py). A
        # server-wide gate here would reject tokens with only one scope,
        # breaking the read-only / write-only token use cases.
    )

    supported_scopes = [effective_read, effective_write]

    if not base_url:
        logger.info(
            "OAuth enabled (token verification only; PRM disabled). scopes=%s",
            supported_scopes,
        )
        return verifier

    auth = RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(issuer)],
        base_url=base_url,
        scopes_supported=supported_scopes,
        resource_name="Couchbase MCP Server",
    )
    logger.info(
        "OAuth enabled with PRM at %s/.well-known/oauth-protected-resource/mcp (scopes=%s)",
        base_url.rstrip("/"),
        supported_scopes,
    )
    return auth
