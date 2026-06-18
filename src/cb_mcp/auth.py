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

# Scopes advertised in the protected-resource metadata document. Per-tool
# enforcement is done inside tool wrappers (see scope_enforcement.py); the
# JWTVerifier itself does not gate at the server level.
SUPPORTED_SCOPES = [SCOPE_READ, SCOPE_WRITE]


def build_oauth(
    *,
    jwks_uri: str,
    issuer: str,
    audience: str,
    algorithm: str = DEFAULT_OAUTH_ALGORITHM,
    base_url: str | None = None,
) -> AuthProvider:
    """Build the FastMCP ``AuthProvider`` for the configured OAuth setup.

    Returns a bare ``JWTVerifier`` when ``base_url`` is omitted — the server
    validates tokens but does not publish protected-resource metadata. Pass
    ``base_url`` to wrap the verifier in a ``RemoteAuthProvider`` that also
    serves RFC 9728 metadata at
    ``<base_url>/.well-known/oauth-protected-resource``, advertising the IdP
    (derived from ``issuer``) and the two supported scopes so PRM-aware
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

    Returns:
        ``AuthProvider`` suitable for passing directly to
        ``FastMCP(auth=...)``.

    Caller contract: the CLI layer is responsible for refusing partial
    configuration before calling this function. No defensive None-checks
    here — passing empty required arguments is a programmer error.
    """
    verifier = JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=issuer,
        audience=audience,
        algorithm=algorithm,
        # required_scopes left None: per-tool enforcement happens inside
        # wrap_with_scope_check (see utils/scope_enforcement.py). A
        # server-wide gate here would reject tokens with only one scope,
        # breaking the read-only / write-only token use cases.
    )

    if not base_url:
        logger.info("OAuth enabled (token verification only; PRM disabled).")
        return verifier

    auth = RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(issuer)],
        base_url=base_url,
        scopes_supported=SUPPORTED_SCOPES,
        resource_name="Couchbase MCP Server",
    )
    logger.info(
        "OAuth enabled with PRM at %s/.well-known/oauth-protected-resource (scopes=%s)",
        base_url.rstrip("/"),
        SUPPORTED_SCOPES,
    )
    return auth
