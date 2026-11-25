"""Unit tests for the shared test-environment helpers (tests/_test_env.py)."""

from __future__ import annotations

from _test_env import missing_cluster_credentials

CONNECTION = {"CB_CONNECTION_STRING": "couchbases://localhost"}
PASSWORD = {"CB_USERNAME": "admin", "CB_PASSWORD": "secret"}
CERT = {"CB_CLIENT_CERT_PATH": "/c/client.pem", "CB_CLIENT_KEY_PATH": "/c/client.key"}


def test_username_and_password_are_enough() -> None:
    assert missing_cluster_credentials({**CONNECTION, **PASSWORD}) == []


def test_client_certificate_replaces_username_and_password() -> None:
    assert missing_cluster_credentials({**CONNECTION, **CERT}) == []


def test_half_a_certificate_is_not_enough() -> None:
    env = {**CONNECTION, "CB_CLIENT_CERT_PATH": "/c/client.pem"}
    assert missing_cluster_credentials(env) == ["CB_USERNAME", "CB_PASSWORD"]


def test_connection_string_is_always_required() -> None:
    assert missing_cluster_credentials({**PASSWORD}) == ["CB_CONNECTION_STRING"]
    assert missing_cluster_credentials({**CERT}) == ["CB_CONNECTION_STRING"]


def test_nothing_set_reports_everything() -> None:
    assert missing_cluster_credentials({}) == [
        "CB_CONNECTION_STRING",
        "CB_USERNAME",
        "CB_PASSWORD",
    ]
