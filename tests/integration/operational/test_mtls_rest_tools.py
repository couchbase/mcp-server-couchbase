"""
Integration tests for client-certificate (mTLS) authentication of the REST-backed tools.

Regression tests for DA-2504 / CBSE-24146. On a cluster that only accepts client
certificates there is no username or password, and the tools that call
Couchbase's REST endpoints directly (rather than through the SDK) used to fail
before sending anything:

    Missing required connection settings: username, password

The server here gets a client certificate and *no* username or password.

They need a cluster set up by ``scripts/setup_mtls_cluster.sh`` and are skipped
unless these variables point at its output (the ``mtls-integration-tests`` CI
job sets them):

    CB_MCP_TEST_MTLS_CERT   client certificate (PEM)
    CB_MCP_TEST_MTLS_KEY    its private key
    CB_MCP_TEST_MTLS_CA     the CA that signed the cluster's node certificate

``CB_MCP_TEST_MTLS_CONNECTION_STRING`` defaults to ``couchbases://127.0.0.1``: an
IPv4 literal, because where ``localhost`` resolves to ``::1`` first the SDK
reports the node as ``[::1]``, which not every Couchbase service listens on.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from conftest import (
    create_session_for_subcommand,
    extract_payload,
    is_error_response,
)

_CERT = os.environ.get("CB_MCP_TEST_MTLS_CERT")
_KEY = os.environ.get("CB_MCP_TEST_MTLS_KEY")
_CA = os.environ.get("CB_MCP_TEST_MTLS_CA")

pytestmark = pytest.mark.skipif(
    not (_CERT and _KEY and _CA),
    reason=(
        "mTLS tests need CB_MCP_TEST_MTLS_CERT, CB_MCP_TEST_MTLS_KEY and "
        "CB_MCP_TEST_MTLS_CA (see scripts/setup_mtls_cluster.sh)"
    ),
)

#: The REST-backed tools from the ticket, with arguments that work on any
#: cluster that has travel-sample loaded. ``delete_active_query`` is given a
#: request that does not exist: the server answering it is enough to show the
#: call was authenticated. ``test_cluster_connection`` goes through the SDK,
#: which always supported certificates, and is the baseline.
TOOL_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("test_cluster_connection", {}),
    ("get_bucket_config", {}),
    (
        "get_cluster_metrics",
        {
            "metrics": [
                {"metric": [{"label": "name", "value": "sysproc_cpu_utilization"}]}
            ]
        },
    ),
    ("get_cluster_tasks", {}),
    ("get_cluster_health_snapshot", {}),
    ("get_cluster_system_events", {}),
    ("get_cluster_query_vitals", {}),
    ("get_active_queries", {}),
    (
        "delete_active_query",
        {"request_id": "00000000-0000-0000-0000-000000000000"},
    ),
    ("list_indexes", {}),
    ("get_index_stats", {}),
]


def _server_env(*, with_certificate: bool = True) -> dict[str, str]:
    """Server environment: a client certificate, and no username/password."""
    env = os.environ.copy()
    for var in ("CB_USERNAME", "CB_PASSWORD"):
        env.pop(var, None)
    env.update(
        {
            "CB_CONNECTION_STRING": os.environ.get(
                "CB_MCP_TEST_MTLS_CONNECTION_STRING", "couchbases://127.0.0.1"
            ),
            "CB_CA_CERT_PATH": _CA or "",
            "CB_MCP_TRANSPORT": "stdio",
            "CB_MCP_READ_ONLY_MODE": "false",
        }
    )
    if with_certificate:
        env["CB_CLIENT_CERT_PATH"] = _CERT or ""
        env["CB_CLIENT_KEY_PATH"] = _KEY or ""
    return env


def _failure(response: Any) -> str | None:
    """Why *response* is a failure, or None. Tools report failure either as an
    MCP error or as a normal response whose payload carries an ``error``."""
    payload = extract_payload(response)
    if is_error_response(response):
        return str(payload)
    if isinstance(payload, dict) and (
        payload.get("status") == "error"
        or payload.get("success") is False
        or payload.get("error")
    ):
        return str(payload)
    return None


@pytest.mark.asyncio
async def test_tools_work_with_client_certificate_only() -> None:
    """Every tool authenticates with the certificate alone."""
    failures: dict[str, str] = {}
    async with create_session_for_subcommand("operational", _server_env()) as session:
        for tool, arguments in TOOL_CALLS:
            reason = _failure(await session.call_tool(tool, arguments=arguments))
            if reason:
                failures[tool] = reason

    assert not failures, "tools failed with a client certificate only:\n" + "\n".join(
        f"  {tool}: {reason}" for tool, reason in failures.items()
    )


@pytest.mark.asyncio
async def test_missing_credentials_error_points_at_the_certificate_option() -> None:
    """With neither a certificate nor a password, the error says what to set."""
    env = _server_env(with_certificate=False)
    async with create_session_for_subcommand("operational", env) as session:
        response = await session.call_tool("get_cluster_tasks")

    assert is_error_response(response)
    message = str(extract_payload(response))
    assert "username, password" in message
    assert "client_cert_path" in message
