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
import logging
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from _all_specs import ALL_SPECS

from cb_mcp.audit import emitter
from cb_mcp.audit.catalog import AuditEvent
from cb_mcp.audit.config import resolve_audit_config
from cb_mcp.audit.emitter import get_audit_logger, init_audit, shutdown_audit
from cb_mcp.audit.sink import process_scoped_path
from cb_mcp.auth import CouchbaseJWTVerifier
from cb_mcp.core.app import _start_audit, build_app


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


def _build_unstarted_app(spec, audit_config, tmp_path):
    """Call ``build_app`` without running its lifespan.

    The warning under test is emitted while the app is being built, not while
    it runs, which is the point: an operator sees it at startup.
    """
    with patch("cb_mcp.core.app.FastMCP", return_value=MagicMock()):
        return build_app(
            spec,
            tools=list(spec.tools.all_tools)[:1],
            settings={"transport": "stdio", "oauth_enabled": False},
            provider_factory=MagicMock(),
            audit_config=audit_config,
        )


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


def test_the_startup_record_states_the_configuration_actually_in_force(tmp_path):
    """The permanent record must not claim a file sink that failed to open.

    57346 is written once and kept for the life of the audit trail. An earlier
    version corrected the logger's own copy of the config but still wrote the
    *requested* one into this record and onto ``AppContext``, so an auditor —
    and ``get_server_configuration_status`` — were told records were going to
    disk when the file sink had been dropped.
    """
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    config = _audit_config(
        blocked, sinks="console,file", file=str(blocked / "audit.log")
    )
    process_scoped_path(config.file).mkdir()

    try:
        effective = _start_audit(
            next(sp for sp in ALL_SPECS if sp.audit_package is not None),
            config,
            transport="stdio",
            oauth_enabled=False,
            read_only_mode=False,
            registered_tool_names=["get_document_by_id"],
            settings={},
        )
    finally:
        shutdown_audit()

    # What _start_audit hands back is what every reporter must use.
    assert effective.sinks == ("console",)
    assert effective.process_file is None
    assert config.sinks == ("console", "file"), "the requested config is unchanged"


def test_the_57346_record_and_app_context_both_state_what_is_in_force(tmp_path, capsys):
    """The two places the snapshot is actually consumed.

    ``_start_audit`` returning the effective config is only half the fix: the
    57346 record is written once and kept forever, and ``AppContext`` is what
    ``get_server_configuration_status`` reports. Asserting only the return
    value left both consumers free to use the requested config instead — and
    both mutations passed the whole suite, producing a permanent record that
    claimed a file sink which had failed to open.
    """
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    config = _audit_config(
        blocked, sinks="console,file", file=str(blocked / "audit.log")
    )
    process_scoped_path(config.file).mkdir()
    spec = next(sp for sp in ALL_SPECS if sp.audit_package is not None)

    captured: dict = {}

    def capture_app(*_args, **kwargs):
        captured["lifespan"] = kwargs.get("lifespan")
        return MagicMock()

    with patch("cb_mcp.core.app.FastMCP", side_effect=capture_app):
        build_app(
            spec,
            tools=list(spec.tools.all_tools)[:1],
            settings={"transport": "stdio", "oauth_enabled": False},
            provider_factory=MagicMock(),
            audit_config=config,
        )

    async def run() -> None:
        async with captured["lifespan"](MagicMock()) as app_context:
            # What the status tool will report.
            assert app_context.audit_config["sinks"] == ["console"]
            assert app_context.audit_config["process_file"] is None

    try:
        asyncio.run(run())
    finally:
        shutdown_audit()

    # What the permanent record says. The file sink was dropped, so the record
    # went to the surviving console sink — which is itself the point.
    emitted = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("{")
    ]
    configuration = next(r for r in emitted if r["name"] == "server configuration")
    assert configuration["audit_config"]["sinks"] == ["console"], configuration
    assert configuration["audit_config"]["process_file"] is None, configuration


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


def test_sigterm_records_a_clean_shutdown_when_nothing_else_handles_it(
    tmp_path, monkeypatch
):
    """``docker stop`` under stdio must not look like a crash in the trail.

    SIGTERM's default disposition terminates the process without running
    ``atexit`` or unwinding the lifespan, so the most ordinary container
    shutdown produced no ``server stopped`` record — while the catalogue says a
    missing one means the shutdown was not clean. Every restart would have read
    as an unclean one, which makes the signal useless for finding a real one.
    """
    config = _audit_config(tmp_path, sinks="file")
    killed: list[int] = []

    audit = init_audit(config)
    assert audit.active
    try:
        # SIG_DFL is the stdio case: nothing else will unwind anything.
        monkeypatch.setattr(emitter, "_previous_sigterm", signal.SIG_DFL)
        monkeypatch.setattr(emitter.signal, "signal", lambda *_a: None)
        monkeypatch.setattr(emitter.os, "kill", lambda _pid, sig: killed.append(sig))
        emitter._handle_sigterm(signal.SIGTERM, None)
    finally:
        shutdown_audit()

    records = _records(tmp_path)
    names = [r["name"] for r in records]
    assert "server stopped" in names, names
    stopped = next(r for r in records if r["name"] == "server stopped")
    assert stopped["outcome"] == "success"
    # Says *how* it stopped, without claiming a failure.
    assert stopped["shutdown_signal"] == "SIGTERM"
    assert "reason" not in stopped
    # Still exits by the signal, so the status code does not pretend it was a
    # normal exit.
    assert killed == [signal.SIGTERM]


def test_sigterm_hands_over_without_closing_when_another_handler_owns_it(
    tmp_path, monkeypatch
):
    """Under http, uvicorn drains in-flight requests *after* its handler runs.

    Closing the audit trail before handing over left every call still running
    unrecorded — the middleware reads the process-wide logger, finds it
    inactive and emits nothing, not even a dropped count. A write cancelled by
    a rolling restart would vanish, which is exactly the suppression that
    catching ``BaseException`` around the tool exists to prevent. The lifespan
    emits ``server stopped`` when uvicorn unwinds it, so handing over loses
    nothing.
    """
    config = _audit_config(tmp_path, sinks="file")
    chained: list[int] = []

    audit = init_audit(config)
    assert audit.active
    try:
        monkeypatch.setattr(
            emitter, "_previous_sigterm", lambda signum, _frame: chained.append(signum)
        )
        emitter._handle_sigterm(signal.SIGTERM, None)

        # The whole point: auditing is still running, so records arriving
        # during the drain are still written.
        assert chained == [signal.SIGTERM]
        assert get_audit_logger().active is True
        get_audit_logger().emit_event(AuditEvent.SESSION_INITIALIZED, outcome="success")
    finally:
        shutdown_audit()

    names = [r["name"] for r in _records(tmp_path)]
    assert "session initialized" in names, (
        f"a record arriving during the drain was lost: {names}"
    )


def test_the_sigterm_handler_is_installed_only_when_auditing_runs(
    tmp_path, monkeypatch
):
    """A server with auditing off must not touch the signal disposition."""
    monkeypatch.setattr(emitter, "_previous_sigterm", None)
    original = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        init_audit(_audit_config(tmp_path, enabled=False))
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL

        init_audit(_audit_config(tmp_path, sinks="file"))
        assert signal.getsignal(signal.SIGTERM) is emitter._handle_sigterm
    finally:
        shutdown_audit()
        signal.signal(signal.SIGTERM, original)


def test_installing_twice_does_not_chain_the_handler_to_itself(tmp_path):
    """The install guard is correctness, not an optimisation.

    ``_handle_sigterm`` hands over to ``_previous_sigterm`` whenever it is
    callable. If a second install captured the already-installed handler as its
    own predecessor, the next SIGTERM would call itself until the stack ran
    out — on the one path that exists to make shutdown *more* reliable.
    """
    original = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        init_audit(_audit_config(tmp_path, sinks="file"))
        shutdown_audit()
        init_audit(_audit_config(tmp_path, sinks="file"))

        assert signal.getsignal(signal.SIGTERM) is emitter._handle_sigterm
        assert emitter._previous_sigterm is not emitter._handle_sigterm
        assert emitter._previous_sigterm is signal.SIG_DFL
    finally:
        shutdown_audit()
        signal.signal(signal.SIGTERM, original)


def test_enabling_auditing_on_an_unaudited_server_warns(tmp_path, caplog):
    """Silence is indistinguishable from a broken sink.

    The Operational Insights server accepts every audit flag and reports it,
    but records nothing. An operator who enabled auditing there would find an
    empty directory, with the only signal being ``active: false`` buried in the
    status tool.
    """
    unaudited = next(sp for sp in ALL_SPECS if sp.audit_package is None)
    config = _audit_config(tmp_path, sinks="file")

    with caplog.at_level(logging.WARNING):
        _build_unstarted_app(unaudited, config, tmp_path)

    assert "declares no audit package" in caplog.text
    assert unaudited.id in caplog.text


def test_an_audited_server_does_not_warn(tmp_path, caplog):
    audited = next(sp for sp in ALL_SPECS if sp.audit_package is not None)
    config = _audit_config(tmp_path, sinks="file")

    with caplog.at_level(logging.WARNING):
        _build_unstarted_app(audited, config, tmp_path)

    assert "declares no audit package" not in caplog.text
