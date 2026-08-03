"""Tests for ``log_environment_info`` — the env-info DEBUG diagnostic record.

The runtime end-to-end behaviour is exercised in ``tests/integration/`` by
spawning the server and grepping the log file. This unit test pins the
JSON-payload contract: documented top-level keys must be present, the
serialised shape must be parseable, and the field types match what the
downstream consumers (support tooling, MCP tool output) expect.
"""

from __future__ import annotations

import json
import logging

import cb_mcp.utils.logging as logmod
from cb_mcp.utils.constants import MCP_SERVER_NAME
from cb_mcp.utils.environment import log_environment_info

ENV_LOGGER_NAME = f"{MCP_SERVER_NAME}.utils.environment"

# Top-level keys ``log_environment_info`` documents and consumers rely on.
# A future refactor that renames or drops one of these will break this test
# immediately rather than silently break support diagnostics later.
EXPECTED_TOP_LEVEL_KEYS = {
    "os",
    "platform",
    "arch",
    "python",
    "mcp_server_version",
    "dependencies",
    "transport",
    "logging",
    "config",
}


def _capture_env_record(server_settings=None) -> logging.LogRecord:
    """Attach a one-shot capture handler and return the emitted record.

    Bypasses configure_logging entirely so the test doesn't fight with the
    Couchbase SDK's one-shot ``configure_logging`` or the global handler state
    that other tests configure. ``server_settings`` defaults to a minimal
    read-only config; pass a dict to exercise specific redaction paths.
    """
    if server_settings is None:
        server_settings = {"read_only_mode": True}
    env_logger = logging.getLogger(ENV_LOGGER_NAME)
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture(level=logging.DEBUG)
    env_logger.addHandler(handler)
    prev_level = env_logger.level
    prev_propagate = env_logger.propagate
    env_logger.setLevel(logging.DEBUG)
    # Stop the record from bubbling to root handlers another test may have
    # configured — otherwise we'd see noise in pytest output and risk
    # tripping caplog-based assertions elsewhere.
    env_logger.propagate = False
    # These tests pin the DEBUG-record payload contract only; force the snapshot
    # to None so ``log_environment_info`` doesn't also try to write a dedicated
    # env file (which would add a second captured record). The file-write path
    # is covered by test_env_snapshot_written_to_dedicated_file.
    prev_resolved = logmod._resolved_config
    logmod._resolved_config = None
    try:
        log_environment_info(transport="http", server_settings=server_settings)
    finally:
        logmod._resolved_config = prev_resolved
        env_logger.removeHandler(handler)
        env_logger.setLevel(prev_level)
        env_logger.propagate = prev_propagate

    assert len(captured) == 1, f"expected exactly one record, got {len(captured)}"
    return captured[0]


def test_record_is_emitted_at_debug_level():
    record = _capture_env_record()
    assert record.levelno == logging.DEBUG


def test_record_carries_environment_prefix_and_json_body():
    record = _capture_env_record()
    msg = record.getMessage()
    assert msg.startswith("Environment | "), (
        f"missing 'Environment | ' prefix:\n{msg[:120]}"
    )
    payload_str = msg.split("Environment | ", 1)[1]
    # The body must be valid JSON — that's the parseability contract.
    json.loads(payload_str)


def test_payload_has_documented_top_level_keys():
    record = _capture_env_record()
    payload = json.loads(record.getMessage().split("Environment | ", 1)[1])
    missing = EXPECTED_TOP_LEVEL_KEYS - payload.keys()
    extra = payload.keys() - EXPECTED_TOP_LEVEL_KEYS
    assert not missing, f"env-info record is missing documented keys: {missing}"
    # ``extra`` is informational — new fields are allowed, but if you're
    # adding one, update ``EXPECTED_TOP_LEVEL_KEYS`` so consumers are aware.
    assert not extra, (
        f"env-info record has undocumented top-level keys: {extra}. "
        f"If intentional, add them to EXPECTED_TOP_LEVEL_KEYS."
    )


def test_payload_field_types_are_stable():
    """Type contract: each documented field must have the expected shape.

    Consumers parse this JSON; a string→int swap (or list→str) would silently
    break them at the type level even if all keys are present.
    """
    record = _capture_env_record()
    payload = json.loads(record.getMessage().split("Environment | ", 1)[1])
    assert isinstance(payload["os"], str)
    assert isinstance(payload["platform"], str)
    assert isinstance(payload["arch"], str)
    assert isinstance(payload["python"], str)
    assert isinstance(payload["mcp_server_version"], str)
    assert isinstance(payload["dependencies"], dict)
    assert all(isinstance(v, str) for v in payload["dependencies"].values()), (
        "dependency versions must be string-valued"
    )
    assert isinstance(payload["transport"], str)
    assert isinstance(payload["config"], dict)
    # ``logging`` may be None when configure_logging hasn't been called yet.
    assert payload["logging"] is None or isinstance(payload["logging"], dict)


def test_transport_value_is_passed_through_verbatim():
    """The transport string the caller passes should appear unchanged."""
    record = _capture_env_record()
    payload = json.loads(record.getMessage().split("Environment | ", 1)[1])
    assert payload["transport"] == "http"


def test_config_block_reflects_redaction_policy():
    """Config block under env-info must apply the same redaction as the MCP tool.

    Specifically: secret/path fields appear only as ``*_configured`` booleans;
    safe-listed scalar keys appear as their literal values.
    """
    record = _capture_env_record()
    payload = json.loads(record.getMessage().split("Environment | ", 1)[1])
    config = payload["config"]
    assert config["read_only_mode"] is True  # value we passed
    # Presence-only redaction is preserved.
    assert "password_configured" in config
    assert config["password_configured"] is False
    assert "ca_cert_path_configured" in config


def test_config_block_captures_oauth_coordinates():
    """OAuth config (non-secret IdP coordinates) is captured verbatim.

    OAuth was added after the logging work; this pins the env record so it
    keeps surfacing OAuth state for support triage.
    """
    record = _capture_env_record(
        server_settings={
            "oauth_enabled": True,
            "oauth_jwks_uri": "https://auth.example.com/.well-known/jwks.json",
            "oauth_issuer": "https://auth.example.com/",
            "oauth_audience": "couchbase-mcp",
            "oauth_algorithm": "RS256",
            "oauth_mcp_base_url": "https://mcp.example.com",
            "oauth_scope_read_label": "couchbase-mcp/read",
            "oauth_scope_write_label": "couchbase-mcp/write",
        }
    )
    config = json.loads(record.getMessage().split("Environment | ", 1)[1])["config"]
    assert config["oauth_enabled"] is True
    assert config["oauth_jwks_uri"] == "https://auth.example.com/.well-known/jwks.json"
    assert config["oauth_issuer"] == "https://auth.example.com/"
    assert config["oauth_audience"] == "couchbase-mcp"
    assert config["oauth_algorithm"] == "RS256"
    assert config["oauth_mcp_base_url"] == "https://mcp.example.com"
    assert config["oauth_scope_read_label"] == "couchbase-mcp/read"
    assert config["oauth_scope_write_label"] == "couchbase-mcp/write"


def _snapshot_with_config_file(config_path) -> logmod.ResolvedLoggingConfig:
    return logmod.ResolvedLoggingConfig(
        level="INFO",
        sinks=("file",),
        log_files={"INFO": "ignored.info.log"},
        log_max_bytes={"INFO": 1048576},
        log_backup_counts={"INFO": 1},
        server_config_file=str(config_path),
    )


def test_config_snapshot_written_to_dedicated_file(tmp_path):
    """When ``server_config_file`` is set, the snapshot is written there as JSON.

    Note the logger is at INFO (not DEBUG) here: the dedicated file must be
    written regardless of level, unlike the DEBUG-gated stderr record.
    """
    config_path = tmp_path / "mcp_server_config.log.json"
    prev = logmod._resolved_config
    logmod._resolved_config = _snapshot_with_config_file(config_path)
    try:
        log_environment_info(
            transport="stdio", server_settings={"read_only_mode": True}
        )
    finally:
        logmod._resolved_config = prev

    assert config_path.exists(), "dedicated config file was not written"
    # The file is pure JSON (no "Environment |" prefix).
    payload = json.loads(config_path.read_text())
    assert payload["transport"] == "stdio"


def test_config_file_overwritten_not_appended(tmp_path):
    """Each start overwrites the file so it always holds the current run — it
    never grows across restarts (immune to rotation by construction)."""
    config_path = tmp_path / "mcp_server_config.log.json"
    prev = logmod._resolved_config
    logmod._resolved_config = _snapshot_with_config_file(config_path)
    try:
        log_environment_info(
            transport="stdio", server_settings={"read_only_mode": True}
        )
        log_environment_info(transport="http", server_settings={"read_only_mode": True})
    finally:
        logmod._resolved_config = prev

    # Still a single valid JSON object (overwritten, not appended), newest run.
    payload = json.loads(config_path.read_text())
    assert payload["transport"] == "http"


def test_no_config_file_written_when_snapshot_absent(tmp_path):
    """With no resolved snapshot (server_config_file None) no file is created."""
    config_path = tmp_path / "mcp_server_config.log.json"
    prev = logmod._resolved_config
    logmod._resolved_config = None
    try:
        log_environment_info(
            transport="stdio", server_settings={"read_only_mode": True}
        )
    finally:
        logmod._resolved_config = prev
    assert not config_path.exists()
