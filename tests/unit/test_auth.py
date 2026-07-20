"""
Unit tests for OAuth / JWT plumbing.

Covers ``build_oauth`` mode selection (PRM vs token-only), per-tool scope
mapping, and the ``wrap_with_scope_check`` decorator's three runtime paths
(token absent → pass through; token holds scope → pass through; token
missing scope → raise).
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier

from cb_mcp.auth import CouchbaseJWTVerifier, build_oauth
from cb_mcp.tools.query import run_sql_plus_plus_query
from cb_mcp.utils.constants import SCOPE_READ, SCOPE_WRITE
from cb_mcp.utils.scope_enforcement import (
    required_scopes_for_tool,
    wrap_with_scope_check,
)

JWKS = "https://idp.example.com/.well-known/jwks.json"
ISSUER = "https://idp.example.com"
AUDIENCE = "mcp-couchbase"


class TestBuildOAuth:
    """build_oauth returns the right FastMCP auth provider for each mode."""

    def test_returns_bare_verifier_without_base_url(self):
        auth = build_oauth(jwks_uri=JWKS, issuer=ISSUER, audience=AUDIENCE)
        assert isinstance(auth, JWTVerifier)
        # No required_scopes — per-tool enforcement is the gate. FastMCP
        # normalizes None to an empty list internally.
        assert not auth.required_scopes

    def test_returns_remote_auth_provider_when_base_url_set(self):
        auth = build_oauth(
            jwks_uri=JWKS,
            issuer=ISSUER,
            audience=AUDIENCE,
            base_url="https://api.example.com",
        )
        assert isinstance(auth, RemoteAuthProvider)
        # PRM should advertise the issuer as the authorization server.
        assert [str(a) for a in auth.authorization_servers] == [
            ISSUER + "/",  # AnyHttpUrl normalizes trailing slash
        ]
        # Both scopes are advertised in PRM.
        assert set(auth._scopes_supported) == {SCOPE_READ, SCOPE_WRITE}

    def test_algorithm_is_forwarded(self):
        auth = build_oauth(
            jwks_uri=JWKS, issuer=ISSUER, audience=AUDIENCE, algorithm="ES256"
        )
        assert auth.algorithm == "ES256"

    def test_default_returns_couchbase_subclass(self):
        """build_oauth must use the normalizing subclass so built-in aliases
        (Cognito slash form, dash form) work without operator config."""
        bare = build_oauth(jwks_uri=JWKS, issuer=ISSUER, audience=AUDIENCE)
        wrapped = build_oauth(
            jwks_uri=JWKS,
            issuer=ISSUER,
            audience=AUDIENCE,
            base_url="https://api.example.com",
        )
        assert isinstance(bare, CouchbaseJWTVerifier)
        assert isinstance(wrapped.token_verifier, CouchbaseJWTVerifier)


class TestCustomScopeNames:
    """``--oauth-scope-read-label`` / ``--oauth-scope-write-label`` let operators
    declare the exact scope strings their IdP issues. They get normalized to
    canonical ``SCOPE_READ`` / ``SCOPE_WRITE`` inside the verifier so
    downstream per-tool enforcement keeps its single canonical view.

    Defaults must preserve current behavior — this is a non-breaking change.
    """

    def test_defaults_advertise_canonical_in_prm(self):
        """No overrides → PRM advertises canonical colon scopes (unchanged)."""
        auth = build_oauth(
            jwks_uri=JWKS,
            issuer=ISSUER,
            audience=AUDIENCE,
            base_url="https://api.example.com",
        )
        assert set(auth._scopes_supported) == {SCOPE_READ, SCOPE_WRITE}

    def test_overrides_advertise_custom_in_prm(self):
        """Override scopes flow into PRM ``scopes_supported`` so the client
        requests them from the IdP."""
        custom_read = "http://api.example.com/mcp/read"
        custom_write = "http://api.example.com/mcp/write"
        auth = build_oauth(
            jwks_uri=JWKS,
            issuer=ISSUER,
            audience=AUDIENCE,
            base_url="https://api.example.com",
            scope_read=custom_read,
            scope_write=custom_write,
        )
        assert set(auth._scopes_supported) == {custom_read, custom_write}

    def test_overrides_normalize_to_canonical(self):
        """Token issued with the operator's scope string should normalize to
        the canonical SCOPE_READ / SCOPE_WRITE for downstream enforcement."""
        custom_read = "http://api.example.com/mcp/read"
        custom_write = "http://api.example.com/mcp/write"
        verifier = build_oauth(
            jwks_uri=JWKS,
            issuer=ISSUER,
            audience=AUDIENCE,
            scope_read=custom_read,
            scope_write=custom_write,
        )
        assert isinstance(verifier, CouchbaseJWTVerifier)

        # Simulate the IdP-issued scope claim. The extractor must surface the
        # canonical strings so the per-tool check (which compares against
        # SCOPE_READ/SCOPE_WRITE) sees them as valid.
        normalized = verifier._extract_scopes(
            {"scope": f"{custom_read} {custom_write}"}
        )
        assert set(normalized) == {SCOPE_READ, SCOPE_WRITE}

    def test_overrides_replace_builtin_aliases(self):
        """A custom label replaces the built-in aliases; canonical scopes still
        pass through."""
        custom_read = "http://api.example.com/mcp/read"
        verifier = build_oauth(
            jwks_uri=JWKS,
            issuer=ISSUER,
            audience=AUDIENCE,
            scope_read=custom_read,
        )
        # Declared custom label normalizes to canonical:
        assert verifier._extract_scopes({"scope": custom_read}) == [SCOPE_READ]
        # Built-in Cognito slash form is not honored; it passes through verbatim:
        assert verifier._extract_scopes({"scope": "couchbase-mcp/write"}) == [
            "couchbase-mcp/write"
        ]
        # Canonical scopes pass through:
        assert verifier._extract_scopes({"scope": SCOPE_READ}) == [SCOPE_READ]
        assert verifier._extract_scopes({"scope": SCOPE_WRITE}) == [SCOPE_WRITE]

    def test_default_extraction_unchanged(self):
        """Without overrides, canonical-form tokens (Auth0/Keycloak/Stytch)
        pass through unchanged — backward compatibility check."""
        verifier = build_oauth(jwks_uri=JWKS, issuer=ISSUER, audience=AUDIENCE)
        scopes = verifier._extract_scopes(
            {"scope": f"{SCOPE_READ} {SCOPE_WRITE} openid"}
        )
        assert set(scopes) == {SCOPE_READ, SCOPE_WRITE, "openid"}

    def test_unknown_scopes_pass_through(self):
        """Anything outside both the override map and built-in aliases must
        be preserved verbatim. Prevents accidental swallowing of third-party
        scopes that happen to coexist in the token."""
        verifier = build_oauth(jwks_uri=JWKS, issuer=ISSUER, audience=AUDIENCE)
        scopes = verifier._extract_scopes({"scope": "openid profile some-other-scope"})
        assert set(scopes) == {"openid", "profile", "some-other-scope"}

    def test_setting_canonical_value_explicitly_is_a_noop(self):
        """Operator explicitly passes the canonical value → no alias entry
        added beyond the built-ins. Smoke test for the conditional in
        ``build_oauth``."""
        # Passing the same value as the default shouldn't change behavior.
        verifier = build_oauth(
            jwks_uri=JWKS,
            issuer=ISSUER,
            audience=AUDIENCE,
            scope_read=SCOPE_READ,
            scope_write=SCOPE_WRITE,
        )
        # Canonical token still works:
        assert verifier._extract_scopes({"scope": SCOPE_READ}) == [SCOPE_READ]


class TestRequiredScopesForTool:
    """KV write tools require SCOPE_WRITE; everything else requires SCOPE_READ."""

    def test_kv_write_tool_requires_write_only(self):
        assert required_scopes_for_tool(
            "upsert_document_by_id",
            write_tool_names={"upsert_document_by_id", "delete_document_by_id"},
        ) == {SCOPE_WRITE}

    def test_read_tool_requires_read_only(self):
        # SQL++ deliberately requires READ — write-only tokens cannot reach it.
        assert required_scopes_for_tool(
            "run_sql_plus_plus_query",
            write_tool_names={"upsert_document_by_id"},
        ) == {SCOPE_READ}

    def test_unknown_tool_defaults_to_read(self):
        # Anything not in the write set falls into the read bucket.
        assert required_scopes_for_tool("some_future_tool", write_tool_names=set()) == {
            SCOPE_READ
        }


@contextmanager
def _mock_token(scopes: list[str] | None):
    """Patch FastMCP's get_access_token at the call site used by the wrapper."""

    class _Tok:
        def __init__(self, scopes_):
            self.scopes = scopes_

    token = _Tok(scopes) if scopes is not None else None
    with patch("cb_mcp.utils.scope_enforcement.get_access_token", return_value=token):
        yield


def _run(coro):
    return asyncio.run(coro)


class TestWrapWithScopeCheck:
    """The wrapper is a no-op without a token, allows valid scopes, and rejects missing scopes."""

    def test_no_token_passes_through(self):
        async def tool(x):
            return x * 2

        wrapped = wrap_with_scope_check(tool, {SCOPE_READ})

        with _mock_token(None):
            assert _run(wrapped(3)) == 6

    def test_matching_scope_passes_through(self):
        async def tool(x):
            return x + 1

        wrapped = wrap_with_scope_check(tool, {SCOPE_READ})

        with _mock_token([SCOPE_READ]):
            assert _run(wrapped(4)) == 5

    def test_missing_scope_raises(self):
        async def tool():
            return "should not run"

        wrapped = wrap_with_scope_check(tool, {SCOPE_WRITE})

        # Token has read, not write — the missing scope drives the rejection.
        with _mock_token([SCOPE_READ]), pytest.raises(PermissionError) as excinfo:
            _run(wrapped())
        assert SCOPE_WRITE in str(excinfo.value)

    def test_strict_semantics_write_only_cannot_call_read(self):
        """SCOPE_WRITE alone does NOT grant read access (per spec)."""

        async def read_tool():
            return "read"

        wrapped = wrap_with_scope_check(read_tool, {SCOPE_READ})

        with _mock_token([SCOPE_WRITE]), pytest.raises(PermissionError):
            _run(wrapped())

    def test_both_scopes_grant_both_categories(self):
        """A token with both scopes can call both categories of tools."""

        async def read_tool():
            return "read"

        async def write_tool():
            return "write"

        wrapped_read = wrap_with_scope_check(read_tool, {SCOPE_READ})
        wrapped_write = wrap_with_scope_check(write_tool, {SCOPE_WRITE})

        with _mock_token([SCOPE_READ, SCOPE_WRITE]):
            assert _run(wrapped_read()) == "read"
            assert _run(wrapped_write()) == "write"

    def test_sync_function_is_supported(self):
        """The wrapper works on both sync and async tool functions."""

        def sync_tool(x):
            return x

        wrapped = wrap_with_scope_check(sync_tool, {SCOPE_READ})

        with _mock_token([SCOPE_READ]):
            assert _run(wrapped(42)) == 42

    def test_hint_is_appended_to_rejection_message(self):
        """A per-tool hint should appear in the PermissionError message.

        Use case: SQL++ is classified as a read tool, so the literal rejection
        for a write-only token says 'missing :read' — accurate but missing
        the broader explanation that SQL++ mutations also need :write. The
        hint closes that UX gap without changing enforcement semantics.
        """

        async def sqlpp():
            return "ran"

        hint = "Read-only queries require :read; mutations additionally need :write."
        wrapped = wrap_with_scope_check(sqlpp, {SCOPE_READ}, hint=hint)

        with _mock_token([SCOPE_WRITE]), pytest.raises(PermissionError) as excinfo:
            _run(wrapped())
        assert hint in str(excinfo.value)


def _ctx_with_modes(read_only_mode: bool = False):
    """Build a Mock Context with the lifespan-context flag set."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context.read_only_mode = read_only_mode
    return ctx


class TestSqlPlusPlusScopeGate:
    """SQL++ tool: read-scoped tokens may SELECT but not mutate.

    The per-tool wrapper classifies run_sql_plus_plus_query as a read tool,
    so :read-only tokens can reach it. The tool itself must add a second
    gate that rejects data/structure mutations unless :write is also held.
    Without this, a :read-only token could escalate to writes via SQL++.
    """

    def test_read_only_token_blocks_insert(self):
        """A token with only SCOPE_READ must NOT be able to INSERT via SQL++."""
        ctx = _ctx_with_modes(read_only_mode=False)
        token = SimpleNamespace(scopes=[SCOPE_READ])

        with (
            patch("cb_mcp.tools.query.get_access_token", return_value=token),
            patch("cb_mcp.tools.query.get_cluster_connection"),
            patch("cb_mcp.tools.query.connect_to_bucket"),
            pytest.raises(PermissionError) as excinfo,
        ):
            run_sql_plus_plus_query(
                ctx,
                "b",
                "s",
                "INSERT INTO c (KEY, VALUE) VALUES ('k', {'a': 1})",
            )
        assert SCOPE_WRITE in str(excinfo.value)

    def test_read_only_token_blocks_ddl(self):
        """Structure-modifying queries (CREATE INDEX, etc.) also require :write."""
        ctx = _ctx_with_modes(read_only_mode=False)
        token = SimpleNamespace(scopes=[SCOPE_READ])

        with (
            patch("cb_mcp.tools.query.get_access_token", return_value=token),
            patch("cb_mcp.tools.query.get_cluster_connection"),
            patch("cb_mcp.tools.query.connect_to_bucket"),
            pytest.raises(PermissionError),
        ):
            run_sql_plus_plus_query(
                ctx,
                "b",
                "s",
                "CREATE INDEX foo ON c(name)",
            )

    def test_both_scopes_allow_writes_through_sqlpp(self):
        """A token with BOTH scopes can mutate via SQL++ (subject to CB RBAC)."""
        ctx = _ctx_with_modes(read_only_mode=False)
        token = SimpleNamespace(scopes=[SCOPE_READ, SCOPE_WRITE])

        # Force scope().query() to raise a sentinel so we can prove execution
        # reached the cluster path rather than being blocked by the gate.
        bucket = MagicMock()
        bucket.scope.return_value.query.side_effect = RuntimeError("reached cluster")

        with (
            patch("cb_mcp.tools.query.get_access_token", return_value=token),
            patch("cb_mcp.tools.query.get_cluster_connection"),
            patch("cb_mcp.tools.query.connect_to_bucket", return_value=bucket),
            pytest.raises(RuntimeError, match="reached cluster"),
        ):
            run_sql_plus_plus_query(
                ctx,
                "b",
                "s",
                "INSERT INTO c (KEY, VALUE) VALUES ('k', {'a': 1})",
            )

    def test_no_token_falls_back_to_config_only(self):
        """Without OAuth (no token), historical config-only behavior applies."""
        # read_only_mode=True should block writes regardless of scope.
        ctx = _ctx_with_modes(read_only_mode=True)

        with (
            patch("cb_mcp.tools.query.get_access_token", return_value=None),
            patch("cb_mcp.tools.query.get_cluster_connection"),
            patch("cb_mcp.tools.query.connect_to_bucket"),
            pytest.raises(ValueError, match="not allowed in read-only mode"),
        ):
            run_sql_plus_plus_query(
                ctx,
                "b",
                "s",
                "INSERT INTO c (KEY, VALUE) VALUES ('k', {'a': 1})",
            )
