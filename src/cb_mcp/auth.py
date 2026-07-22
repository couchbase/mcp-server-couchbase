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
from pydantic import AnyHttpUrl, ValidationError

from .utils.constants import (
    DEFAULT_OAUTH_ALGORITHM,
    MCP_SERVER_NAME,
    SCOPE_READ,
    SCOPE_WRITE,
    STREAMABLE_HTTP_TRANSPORT,
)

logger = logging.getLogger(f"{MCP_SERVER_NAME}.auth")


class OAuthConfigError(Exception):
    """Invalid or incomplete CLI/env OAuth settings.

    Framework-agnostic; the CLI layer re-raises it as a ``click.UsageError``.
    """


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

    Operator-supplied ``scope_aliases`` (via CLI / env) replace the built-in
    aliases; the built-ins apply when none are given.
    """

    # Default aliases for the common ``couchbase-mcp``-prefixed variants,
    # used when the operator supplies no ``scope_aliases``.
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
            scope_aliases: Optional map from IdP-issued scope strings to
                canonical scopes, for IdPs that cannot emit the canonical form.
                Replaces the built-in aliases when given; the built-ins apply
                otherwise.
            **jwt_verifier_kwargs: Forwarded to ``JWTVerifier.__init__``
                (``jwks_uri``, ``issuer``, ``audience``, ``algorithm``, etc.).
        """
        super().__init__(**jwt_verifier_kwargs)
        # Operator aliases replace the built-ins; canonical scopes pass through.
        self._scope_aliases: dict[str, str] = dict(
            scope_aliases or self._BUILTIN_ALIASES
        )

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

    # Alias entries only for non-canonical overrides. A non-empty map replaces
    # the built-ins, so override both labels if the IdP uses non-canonical
    # forms for both scopes.
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


def resolve_oauth(
    *,
    transport: str,
    jwks_uri: str | None,
    issuer: str | None,
    audience: str | None,
    algorithm: str,
    base_url: str | None,
    scope_read: str | None = None,
    scope_write: str | None = None,
) -> AuthProvider | None:
    """Resolve CLI/env OAuth settings into a FastMCP ``AuthProvider`` or ``None``.

    Contract:
      - OAuth is honored only when ``transport`` is the streamable-http
        transport. For any other transport (stdio, sse), OAuth settings —
        if any are provided — are ignored with a warning, and ``None`` is
        returned.
      - If none of the three required JWT settings are provided, OAuth is
        opt-in via absence: returns ``None`` silently.
      - If the user provides some but not all of (jwks_uri, issuer,
        audience), raise ``OAuthConfigError`` so misconfiguration fails
        loud instead of silently disabling auth.
      - ``algorithm`` always has a default and isn't part of the
        all-or-nothing check.
      - When ``base_url`` is set, ``issuer`` is published in PRM as an
        authorization server and must be a valid http(s) URL. We validate
        that here (rather than letting the Pydantic ``AnyHttpUrl`` coercion
        inside ``build_oauth`` raise a raw traceback) so the caller gets a
        clear ``OAuthConfigError``. Token-only mode does not require a URL
        issuer, matching ``JWTVerifier``'s plain-string ``iss`` handling.

    Raises:
        OAuthConfigError: on incomplete or invalid configuration. The CLI
            layer translates this into a ``click.UsageError`` so the operator
            gets a clean usage message.
    """
    jwt_fields = {
        "--oauth-jwks-uri / CB_MCP_OAUTH_JWT_JWKS_URI": jwks_uri,
        "--oauth-issuer / CB_MCP_OAUTH_JWT_ISSUER": issuer,
        "--oauth-audience / CB_MCP_OAUTH_JWT_AUDIENCE": audience,
    }
    provided = {k: v for k, v in jwt_fields.items() if v}
    any_provided = bool(provided)
    any_oauth_setting = any_provided or bool(base_url)

    if transport != STREAMABLE_HTTP_TRANSPORT:
        if any_oauth_setting:
            logger.warning(
                "OAuth settings provided but transport=%s; OAuth is only honored "
                "for streamable-http (--transport=http). Ignoring OAuth config.",
                transport,
            )
        return None

    if not any_provided:
        if base_url:
            logger.warning(
                "CB_MCP_OAUTH_MCP_BASE_URL set without any JWT settings; "
                "ignoring (PRM publication requires a configured token verifier)."
            )
        logger.info("OAuth disabled (no CB_MCP_OAUTH_JWT_* settings provided).")
        return None

    if len(provided) != len(jwt_fields):
        missing = sorted(set(jwt_fields) - set(provided))
        raise OAuthConfigError(
            "Incomplete OAuth configuration. To enable OAuth, set all of: "
            + ", ".join(jwt_fields)
            + f". Missing: {missing}."
        )

    if base_url:
        try:
            AnyHttpUrl(issuer)
        except ValidationError as e:
            raise OAuthConfigError(
                f"--oauth-issuer / CB_MCP_OAUTH_JWT_ISSUER must be a valid "
                f"http(s) URL when --oauth-mcp-base-url is set, because the "
                f"issuer is published in Protected Resource Metadata as an "
                f"authorization server. Got: {issuer!r}."
            ) from e

    # The read and write labels must be distinct — mirror build_oauth's
    # effective-value resolution so a None argument still maps to its
    # canonical default before the comparison.
    effective_read = scope_read or SCOPE_READ
    effective_write = scope_write or SCOPE_WRITE
    if effective_read == effective_write:
        raise OAuthConfigError(
            "The read and write OAuth scope labels must be distinct, but both "
            f"resolve to {effective_read!r}. Set different values for "
            "--oauth-scope-read-label / --oauth-scope-write-label "
            "(CB_MCP_OAUTH_SCOPE_READ_LABEL / CB_MCP_OAUTH_SCOPE_WRITE_LABEL); "
            "note a single override that equals the other scope's canonical "
            "default also collides."
        )

    return build_oauth(
        jwks_uri=jwks_uri,
        issuer=issuer,
        audience=audience,
        algorithm=algorithm,
        base_url=base_url,
        scope_read=scope_read,
        scope_write=scope_write,
    )
