"""Unit tests for get_server_configuration_status tool payload."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from fastmcp import Context

from cb_mcp.tools.server import get_server_configuration_status


def _make_ctx(settings=None, cluster_provider=None, logging_config=None) -> Context:
    # SimpleNamespace duck-types the bits get_server_configuration_status
    # actually reads (request_context.lifespan_context.{settings,
    # cluster_provider, logging_config}); the cast tells pyright that's
    # fine for these tests — building a real fastmcp Context would mean
    # standing up a server and a request, which is far more setup than
    # the assertions warrant.
    return cast(
        Context,
        SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context=SimpleNamespace(
                    cluster_provider=cluster_provider,
                    settings=settings if settings is not None else {},
                    logging_config=logging_config,
                )
            )
        ),
    )


def test_configuration_status_exposes_tool_lists():
    ctx = _make_ctx(
        {
            "connection_string": "couchbases://example",
            "username": "test-user",
            "read_only_mode": True,
            "disabled_tools": {"z_tool", "a_tool"},
            "confirmation_required_tools": {
                "delete_document_by_id",
                "replace_document_by_id",
            },
        }
    )

    payload = get_server_configuration_status(ctx)
    config = payload["configuration"]

    assert config["disabled_tools"] == ["a_tool", "z_tool"]
    assert config["confirmation_required_tools"] == [
        "delete_document_by_id",
        "replace_document_by_id",
    ]


def test_configuration_status_defaults_tool_lists_to_empty():
    payload = get_server_configuration_status(_make_ctx())
    config = payload["configuration"]

    assert config["disabled_tools"] == []
    assert config["confirmation_required_tools"] == []


def test_configuration_status_exposes_oauth_config():
    """OAuth resource-server config surfaces (non-secret IdP coordinates).

    Mirrors the env-info diagnostic record so support sees the same OAuth
    state in both the log file and the MCP tool response.
    """
    ctx = _make_ctx(
        {
            "oauth_enabled": True,
            "oauth_jwks_uri": "https://auth.example.com/.well-known/jwks.json",
            "oauth_issuer": "https://auth.example.com/",
            "oauth_audience": "couchbase-mcp",
            "oauth_algorithm": "RS256",
            "oauth_mcp_base_url": "https://mcp.example.com",
        }
    )

    config = get_server_configuration_status(ctx)["configuration"]

    assert config["oauth_enabled"] is True
    assert config["oauth_jwks_uri"] == "https://auth.example.com/.well-known/jwks.json"
    assert config["oauth_issuer"] == "https://auth.example.com/"
    assert config["oauth_audience"] == "couchbase-mcp"
    assert config["oauth_algorithm"] == "RS256"
    assert config["oauth_mcp_base_url"] == "https://mcp.example.com"


def test_configuration_status_oauth_defaults_when_unset():
    """With no OAuth settings, enabled is False and coordinates are None."""
    config = get_server_configuration_status(_make_ctx())["configuration"]

    assert config["oauth_enabled"] is False
    assert config["oauth_jwks_uri"] is None
    assert config["oauth_issuer"] is None
    assert config["oauth_audience"] is None
    assert config["oauth_mcp_base_url"] is None


def test_logging_block_passed_through_from_lifespan_context():
    """The tool surfaces whatever shape AppContext.logging_config carries.

    The tool itself has no dependency on the logging module — it just reads
    the dict the host server entrypoint placed on the lifespan context. This
    keeps the tool reusable across MCP server implementations that may use
    different logging stacks.
    """
    logging_snapshot = {
        "level": "DEBUG",
        "sinks": ["file", "stderr"],
        "log_files": {
            "DEBUG": "/var/log/mcp.debug.log",
            "INFO": "/var/log/mcp.info.log",
            "WARNING": "/var/log/mcp.warning.log",
            "ERROR": "/var/log/mcp.error.log",
        },
        "max_bytes": 1048576,
    }
    payload = get_server_configuration_status(
        _make_ctx(logging_config=logging_snapshot)
    )
    assert payload["logging"] == logging_snapshot


def test_logging_block_is_none_when_lifespan_omits_it():
    """A host server that doesn't populate logging_config gets a clean ``None``.

    Decoupling check: a third-party implementation using a different logging
    stack may use a lifespan-context type that doesn't even *declare* a
    ``logging_config`` attribute. ``get_logging_config()`` uses ``getattr``
    with a default, so the tool degrades to ``"logging": null`` without
    raising ``AttributeError``.
    """
    # Build a lifespan_context with no logging_config attribute at all —
    # this exercises the missing-attribute path, not just the value-is-None
    # path. _make_ctx() always sets the field, so we build the ctx by hand.
    lifespan = SimpleNamespace(cluster_provider=None, settings={})
    assert not hasattr(lifespan, "logging_config")
    ctx = cast(
        Context,
        SimpleNamespace(request_context=SimpleNamespace(lifespan_context=lifespan)),
    )

    payload = get_server_configuration_status(ctx)
    assert payload["logging"] is None


def test_logging_block_alongside_existing_configuration_keys():
    """The new logging block is a peer of configuration/connections, not nested."""
    payload = get_server_configuration_status(
        _make_ctx(
            settings={"read_only_mode": True},
            logging_config={"level": "INFO", "sinks": ["stderr"]},
        )
    )
    assert "logging" in payload
    assert "configuration" in payload
    assert "connections" in payload
    # logging is NOT inside configuration — it's a top-level peer.
    assert "logging" not in payload["configuration"]
