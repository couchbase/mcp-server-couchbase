"""Tests for the redaction policy used by the env-info diagnostic record.

This is security-sensitive code: the env-info log lands in support bundles,
log aggregators, and screenshots. Anything that leaks here would leak there.
The redaction relies on an explicit allow-list + a presence-only list; any
settings key not in either is silently dropped. These tests assert that
contract end-to-end, including the "future settings key" failure mode where
an unclassified field would otherwise leak by default.
"""

from __future__ import annotations

import json

from cb_mcp.utils.environment import _redacted_settings


def test_safe_keys_pass_through_verbatim():
    out = _redacted_settings(
        {
            "read_only_mode": True,
            "transport": "http",
            "host": "127.0.0.1",
            "port": 8000,
            "connection_string": "couchbase://example",
            "username": "admin",
        }
    )
    assert out["read_only_mode"] is True
    assert out["transport"] == "http"
    assert out["host"] == "127.0.0.1"
    assert out["port"] == 8000
    assert out["connection_string"] == "couchbase://example"

def test_secret_paths_redacted_to_presence_booleans():
    """Missing safe keys map to ``None``; presence-only keys map to ``True``/``False``.

    The ``None`` for iterable keys (``disabled_tools``, etc.) is intentional:
    it distinguishes "operator never configured this" from "operator set this
    to an empty collection" (which would serialise as ``[]``).
    """
    out = _redacted_settings(
        {
            "password": "hunter2",
            "ca_cert_path": "/etc/ssl/ca.pem",
            "client_cert_path": "/etc/ssl/client.pem",
            "client_key_path": "/etc/ssl/client.key",
        }
    )
    assert out == {
        # all safe keys absent in input → fall through as None
        "read_only_mode": None,
        "transport": None,
        "host": None,
        "port": None,
        "connection_string": None,
        "disabled_tools": None,
        "confirmation_required_tools": None,
        # OAuth coordinates: safe keys, absent in input → None
        "oauth_enabled": None,
        "oauth_jwks_uri": None,
        "oauth_issuer": None,
        "oauth_audience": None,
        "oauth_algorithm": None,
        "oauth_mcp_base_url": None,
        # presence-only keys: values redacted to booleans
        "password_configured": True,
        "ca_cert_path_configured": True,
        "client_cert_path_configured": True,
        "client_key_path_configured": True,
    }


def test_unset_presence_only_keys_report_false():
    out = _redacted_settings({})
    assert out["password_configured"] is False
    assert out["ca_cert_path_configured"] is False
    assert out["client_cert_path_configured"] is False
    assert out["client_key_path_configured"] is False


def test_empty_string_secrets_treated_as_unset():
    """An empty string for a presence-only key reports `*_configured: False`.

    Catches the "user passed --password '' by accident" case so we don't
    falsely advertise that a secret is set.
    """
    out = _redacted_settings({"password": "", "ca_cert_path": ""})
    assert out["password_configured"] is False
    assert out["ca_cert_path_configured"] is False


def test_iterables_normalised_to_sorted_lists():
    """Tool name iterables are sorted so the env-info record is stable across runs.

    Without normalisation, a set's iteration order would change the JSON output
    between runs, making log-diff comparisons noisy.
    """
    out = _redacted_settings(
        {
            "disabled_tools": {"z_tool", "a_tool", "m_tool"},
            "confirmation_required_tools": ["replace", "delete"],
        }
    )
    assert out["disabled_tools"] == ["a_tool", "m_tool", "z_tool"]
    assert out["confirmation_required_tools"] == ["delete", "replace"]


def test_iterables_accept_sets_lists_tuples_frozensets():
    """Normalisation covers all four iterable types we might see in settings."""
    for collection in (
        {"b", "a"},
        ["b", "a"],
        ("b", "a"),
        frozenset({"b", "a"}),
    ):
        out = _redacted_settings({"disabled_tools": collection})
        assert out["disabled_tools"] == ["a", "b"]


def test_unknown_keys_are_silently_dropped():
    """Allow-list semantics: a brand-new settings key never leaks by default.

    This is the key security property — a future field added to the settings
    dict must be explicitly classified into _SAFE_SETTINGS_KEYS or
    _PRESENCE_ONLY_KEYS to appear in the env-info record.
    """
    out = _redacted_settings(
        {
            "oauth_token": "very-secret",
            "api_key": "also-secret",
            "future_feature_flag": True,
        }
    )
    # The keys themselves are dropped (not "future_feature_flag_configured" — only
    # the explicit allow-list entries get the "_configured" suffix treatment).
    assert "oauth_token" not in out
    assert "api_key" not in out
    assert "future_feature_flag" not in out


def test_secrets_never_appear_in_serialised_output():
    """End-to-end paranoia check: no input-value string survives serialisation.

    The env-info record is emitted via json.dumps(), so this asserts the
    *serialised* form too — guarding against a hypothetical regression where
    a __repr__ or default= leaks something.
    """
    secrets = {
        "password": "p@ssw0rd-very-distinctive",
        "ca_cert_path": "/secret/path/ca.pem-very-distinctive",
        "client_cert_path": "/secret/path/client.pem-very-distinctive",
        "client_key_path": "/secret/path/client.key-very-distinctive",
        "future_secret": "should-never-leak-very-distinctive",
    }
    out = _redacted_settings(secrets)
    serialised = json.dumps(out, default=str)
    for value in secrets.values():
        assert value not in serialised, f"value leaked into output: {value!r}"
