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
    try:
        log_environment_info(transport="http", server_settings=server_settings)
    finally:
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
        }
    )
    config = json.loads(record.getMessage().split("Environment | ", 1)[1])["config"]
    assert config["oauth_enabled"] is True
    assert config["oauth_jwks_uri"] == "https://auth.example.com/.well-known/jwks.json"
    assert config["oauth_issuer"] == "https://auth.example.com/"
    assert config["oauth_audience"] == "couchbase-mcp"
    assert config["oauth_algorithm"] == "RS256"
    assert config["oauth_mcp_base_url"] == "https://mcp.example.com"
