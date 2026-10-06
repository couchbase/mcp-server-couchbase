"""Production wiring for auditing: the paths no unit test injected its own logger into.

Every other audit test builds its own ``AuditLogger`` and its own ``FastMCP``,
which is right for isolating behaviour but leaves the actual assembly
unexercised. These tests drive the real seams instead:

- ``build_app`` -> ``_start_audit`` -> the startup records, and the shutdown
  record the lifespan's ``finally`` emits.
- That a server declaring no ``audit_package`` registers no middleware and
  writes nothing, which is what keeps Operational Insights out of the
  operational Tier-2 block.
- ``CouchbaseJWTVerifier.verify_token`` emitting ``token rejected`` — the one
  audit hook that cannot live in middleware, because a rejected token never
  becomes a JSON-RPC message.
- The six audit flags reaching a resolved configuration through Click.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from _all_specs import ALL_SPECS

from cb_mcp.audit.config import resolve_audit_config
from cb_mcp.audit.emitter import get_audit_logger, init_audit, shutdown_audit
from cb_mcp.audit.sink import process_scoped_path
from cb_mcp.auth import CouchbaseJWTVerifier
from cb_mcp.core.app import build_app


def _records(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in sorted(directory.iterdir())
        if ".log" in path.name
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _audit_config(tmp_path: Path, **overrides):
    options = {
        "enabled": True,
        # The file sink specifically: these tests assert on what reaches disk.
        "sinks": "file",
        "file": str(tmp_path / "audit.log"),
        "rotation_max_size_mb": None,
        "max_backups": None,
        "tool_args": None,
        "disabled_events": None,
    }
    options.update(overrides)
    return resolve_audit_config(**options)


def _run_lifespan(spec, audit_config, tmp_path):
    """Build the app for ``spec`` and take its lifespan through a clean cycle."""
    captured: dict = {}
    added: list = []

    def capture_app(*_args, **kwargs):
        captured["lifespan"] = kwargs.get("lifespan")
        app = MagicMock()
        app.add_middleware.side_effect = added.append
        return app

    with patch("cb_mcp.core.app.FastMCP", side_effect=capture_app):
        build_app(
            spec,
            tools=list(spec.tools.all_tools)[:2],
            settings={
                "transport": "stdio",
                "oauth_enabled": False,
                "disabled_tools": {"drop_index"},
                "confirmation_required_tools": set(),
            },
            provider_factory=MagicMock,
            read_only_mode=True,
            audit_config=audit_config,
        )

    async def drive():
        async with captured["lifespan"](MagicMock()):
            pass

    asyncio.run(drive())
    shutdown_audit()
    return added


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.id)
def test_audit_wiring_follows_the_spec_s_audit_package(spec, tmp_path):
    """Auditing is opt-in per server, and the opt-out is total.

    A server with no ``audit_package`` must register no middleware and write
    no records: its tools would otherwise fall through classification's
    fail-closed path and be booked against another server's Tier-2 block,
    where the same ids mean different operations.
    """
    added = _run_lifespan(spec, _audit_config(tmp_path), tmp_path)
    records = _records(tmp_path)

    if spec.audit_package is None:
        assert added == [], f"{spec.id} registered audit middleware"
        assert records == [], f"{spec.id} wrote audit records"
        return

    assert [type(mw).__name__ for mw in added] == ["AuditMiddleware"]
    assert [record["name"] for record in records] == [
        "server started",
        "server configuration",
        "server stopped",
    ]


def test_startup_records_report_the_enforced_surface(tmp_path):
    """The configuration record is what makes withheld tools auditable.

    Neither a disabled nor a read-only-withheld tool is ever registered with
    FastMCP, so a client is never told it exists and there is no invocation to
    refuse. This record is the only place that surface is stated.
    """
    spec = next(s for s in ALL_SPECS if s.audit_package is not None)
    _run_lifespan(spec, _audit_config(tmp_path), tmp_path)

    config_record = next(
        r for r in _records(tmp_path) if r["name"] == "server configuration"
    )
    assert config_record["server_id"] == spec.id
    assert config_record["service_package"] == spec.audit_package
    assert config_record["read_only_mode"] is True
    assert config_record["oauth_enabled"] is False
    assert config_record["disabled_tools"] == ["drop_index"]
    assert config_record["registered_tools"]
    assert config_record["audit_config"]["enabled"] is True


def test_a_clean_shutdown_is_recorded_as_success(tmp_path):
    spec = next(s for s in ALL_SPECS if s.audit_package is not None)
    _run_lifespan(spec, _audit_config(tmp_path), tmp_path)

    stopped = next(r for r in _records(tmp_path) if r["name"] == "server stopped")
    assert stopped["outcome"] == "success"
    assert "reason" not in stopped


def test_nothing_is_written_when_auditing_is_configured_off(tmp_path):
    """The middleware is still registered; it just never has a sink to write to.

    Registration is deliberately not conditional on the flag: the middleware
    short-circuits on an inactive logger, so one registration path serves both
    modes and a sink that fails to *open* degrades to no records rather than to
    a half-instrumented server. What must hold is that nothing is written.
    """
    spec = next(s for s in ALL_SPECS if s.audit_package is not None)
    _run_lifespan(spec, _audit_config(tmp_path, enabled=False), tmp_path)
    assert not get_audit_logger().active
    assert _records(tmp_path) == []


# ---------------------------------------------------------------------------
# the verifier hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_token_is_audited_by_the_verifier(tmp_path):
    """The one audit hook that cannot live in middleware.

    A rejected bearer token is refused in the ASGI auth layer, so no JSON-RPC
    message is ever constructed and the MCP pipeline never runs. Without this
    override the single most security-relevant event in the catalogue would
    never be emitted.
    """
    init_audit(_audit_config(tmp_path))
    try:
        verifier = CouchbaseJWTVerifier(
            jwks_uri="https://idp.example.com/.well-known/jwks.json",
            issuer="https://idp.example.com/",
            audience="couchbase-mcp",
        )
        with patch.object(
            CouchbaseJWTVerifier.__bases__[0], "verify_token", return_value=None
        ):
            assert await verifier.verify_token("not-a-real-token") is None
    finally:
        shutdown_audit()

    rejected = [r for r in _records(tmp_path) if r["name"] == "token rejected"]
    assert len(rejected) == 1, _records(tmp_path)
    record = rejected[0]
    assert record["id"] == 57376
    assert record["outcome"] == "denied"
    assert record["reason"] == "token_invalid"
    # A rejected token establishes no identity, and the record is a deliberate
    # singleton: it still carries a cid so grouping a file by cid works
    # unconditionally, but nothing else can ever share it.
    assert record["real_userid"] == {"domain": "anonymous", "user": "anonymous"}
    assert record["cid"]


@pytest.mark.asyncio
async def test_an_accepted_token_emits_no_record(tmp_path):
    """There is no "token accepted" event; success is silent at this layer."""
    init_audit(_audit_config(tmp_path))
    try:
        verifier = CouchbaseJWTVerifier(
            jwks_uri="https://idp.example.com/.well-known/jwks.json",
            issuer="https://idp.example.com/",
            audience="couchbase-mcp",
        )
        with patch.object(
            CouchbaseJWTVerifier.__bases__[0], "verify_token", return_value=MagicMock()
        ):
            assert await verifier.verify_token("a-valid-token") is not None
    finally:
        shutdown_audit()

    assert [r for r in _records(tmp_path) if r["name"] == "token rejected"] == []


def test_a_file_sink_that_cannot_be_opened_is_removed_from_the_snapshot(tmp_path):
    """The reported configuration must match what is actually being written.

    ``init_audit`` reports an unopenable file and keeps serving — but the
    snapshot it hands the logger is reported by
    ``get_server_configuration_status`` *and* written into the permanent
    ``audit configuration changed`` record. A snapshot still naming a file sink
    that failed to open would tell an auditor, for as long as the record is
    kept, that records were being written to disk when none were.
    """
    # A directory where the process-scoped file should be: the portable way to
    # make open() fail without depending on the test user's privileges.
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    config = _audit_config(
        blocked, sinks="console,file", file=str(blocked / "audit.log")
    )
    process_scoped_path(config.file).mkdir()

    try:
        audit = init_audit(config)
        # The console sink survives, so auditing is still active...
        assert audit.active is True
        snapshot = audit.config.as_dict()
        # ... but nothing claims a file is being written.
        assert snapshot["sinks"] == ["console"]
        assert snapshot["process_file"] is None
    finally:
        shutdown_audit()


def test_auditing_reports_itself_off_when_every_sink_fails(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    config = _audit_config(blocked, sinks="file", file=str(blocked / "audit.log"))
    process_scoped_path(config.file).mkdir()

    try:
        audit = init_audit(config)
        assert audit.active is False
        assert audit.config.as_dict()["enabled"] is False
    finally:
        shutdown_audit()
