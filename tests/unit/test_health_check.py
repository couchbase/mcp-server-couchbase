"""The ``GET /health`` liveness endpoint served next to the MCP endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from _all_specs import ALL_SPECS

from cb_mcp.auth import resolve_oauth
from cb_mcp.core.app import build_app
from cb_mcp.utils.constants import HEALTH_CHECK_PATH


def _client(spec, *, auth=None, stateless_http=False) -> httpx.AsyncClient:
    mcp = build_app(
        spec,
        tools=[],
        settings={"transport": "http"},
        provider_factory=MagicMock,
        auth=auth,
    )
    # ASGITransport never runs the app's lifespan, so no cluster provider is
    # ever created — which is exactly the point: health must answer without
    # the backing service.
    app = mcp.http_app(stateless_http=stateless_http)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.id)
@pytest.mark.parametrize("stateless_http", [False, True], ids=["stateful", "stateless"])
async def test_health_answers_without_touching_the_cluster(spec, stateless_http):
    async with _client(spec, stateless_http=stateless_http) as client:
        response = await client.get(HEALTH_CHECK_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "server": spec.id}


@pytest.mark.asyncio
async def test_health_is_unauthenticated_when_oauth_protects_mcp():
    """Probes carry no token, so health must stay open while /mcp is guarded."""
    spec = ALL_SPECS[0]
    auth = resolve_oauth(
        transport="http",
        jwks_uri="https://idp.example/.well-known/jwks.json",
        issuer="https://idp.example/",
        audience="couchbase-mcp",
        algorithm="RS256",
        base_url="http://127.0.0.1:8000",
        scope_read="couchbase-mcp:read",
        scope_write="couchbase-mcp:write",
        resource_name=spec.display_name,
    )
    assert auth is not None

    async with _client(spec, auth=auth) as client:
        assert (await client.get(HEALTH_CHECK_PATH)).status_code == 200
        assert (await client.post("/mcp", json={})).status_code == 401


@pytest.mark.asyncio
async def test_health_rejects_other_methods():
    async with _client(ALL_SPECS[0]) as client:
        assert (await client.post(HEALTH_CHECK_PATH)).status_code == 405
