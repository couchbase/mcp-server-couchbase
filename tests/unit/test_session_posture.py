"""The declared deployment posture, and the configurations it refuses.

``cb_mcp.utils.posture`` exists because two deployments of this server differ
in a fact nothing else records: whether a human is at the MCP client when a
tool is called. A confirmation prompt is a control in one and decoration in the
other, and the difference is invisible at runtime -- the prompt is answered
either way.

So the tests below are mostly about refusals. Each one pins a configuration
that contradicts its own declaration and asserts the server will not start
under it, because the value of the flag is entirely in what it rejects. The
accepted combinations are pinned too, so tightening a rule cannot quietly
start refusing a deployment that is fine.

The CLI-level tests at the end are what prove the wiring: a module that
validates correctly but is never called from ``_start_server`` would pass every
unit test above them.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

import couchbase
import pytest
from click.testing import CliRunner

import mcp_server
from cb_mcp.utils.constants import (
    ALLOWED_SESSION_POSTURES,
    NETWORK_TRANSPORTS,
    SESSION_POSTURE_ENTERPRISE,
    SESSION_POSTURE_WORKSTATION,
)
from cb_mcp.utils.posture import (
    PostureConfigError,
    human_is_present,
    validate_posture,
)

# One coherent configuration per posture, so "is every posture reachable" can
# be asserted rather than assumed.
COHERENT = {
    SESSION_POSTURE_WORKSTATION: {"transport": "stdio", "oauth_enabled": False},
    SESSION_POSTURE_ENTERPRISE: {"transport": "http", "oauth_enabled": True},
}


@pytest.fixture(autouse=True)
def mock_sdk_configure_logging():
    """``couchbase.configure_logging`` accepts one call per process.

    The CLI-level tests below start the operational server, whose
    ``sdk_log_hook`` calls it. Patched for the whole module so test order
    cannot matter.
    """
    with patch.object(couchbase, "configure_logging"):
        yield


# --- the presence predicate ----------------------------------------------------


def test_stdio_means_a_human_is_present():
    """stdio is a child process of one interactive client, and dies with it."""
    assert human_is_present("stdio") is True


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_network_transports_mean_nobody_is_watching(transport):
    """A listening server answers whoever connects, which is not a person."""
    assert human_is_present(transport) is False


def test_presence_reads_the_transport_not_the_label():
    """The whole point: a label cannot make an unattended deployment attended.

    If this ever becomes a function of the declared posture, the posture
    becomes self-certifying and every check below becomes a tautology.
    """
    assert human_is_present("http") is False
    assert human_is_present("stdio") is True


# --- undeclared ----------------------------------------------------------------


def test_undeclared_posture_is_not_an_error(caplog):
    """The flag postdates every shipped deployment; requiring it would break them."""
    with caplog.at_level(logging.INFO, logger="couchbase.mcp.utils.posture"):
        assert validate_posture(None, transport="stdio", oauth_enabled=False) is None
    assert any("No deployment posture declared" in r.message for r in caplog.records)


@pytest.mark.parametrize("transport", ["stdio", *NETWORK_TRANSPORTS])
def test_undeclared_posture_accepts_every_transport(transport):
    """Nothing is checked when nothing was declared, under any configuration."""
    assert validate_posture(None, transport=transport, oauth_enabled=False) is None


# --- workstation ---------------------------------------------------------------


def test_workstation_on_stdio_is_accepted():
    assert (
        validate_posture(
            SESSION_POSTURE_WORKSTATION, transport="stdio", oauth_enabled=False
        )
        == SESSION_POSTURE_WORKSTATION
    )


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_workstation_on_a_network_transport_is_refused(transport):
    """A server listening on a port is not one person's workstation."""
    with pytest.raises(PostureConfigError) as excinfo:
        validate_posture(
            SESSION_POSTURE_WORKSTATION, transport=transport, oauth_enabled=False
        )
    message = str(excinfo.value)
    assert f"--transport={transport}" in message
    # The message must carry both ways out, or it is a complaint rather than
    # an error an operator can act on.
    assert "--transport=stdio" in message
    assert SESSION_POSTURE_ENTERPRISE in message


def test_workstation_may_require_confirmation():
    """Confirmation is a real second look when a person is there, so it is allowed."""
    assert (
        validate_posture(
            SESSION_POSTURE_WORKSTATION,
            transport="stdio",
            oauth_enabled=False,
            confirmation_required_tools={"upsert_document_by_id"},
        )
        == SESSION_POSTURE_WORKSTATION
    )


# --- enterprise ----------------------------------------------------------------


def test_enterprise_on_a_network_transport_with_oauth_is_accepted():
    assert (
        validate_posture(
            SESSION_POSTURE_ENTERPRISE, transport="http", oauth_enabled=True
        )
        == SESSION_POSTURE_ENTERPRISE
    )


def test_enterprise_on_stdio_is_refused():
    """stdio is one interactive client's child process, not a service."""
    with pytest.raises(PostureConfigError) as excinfo:
        validate_posture(
            SESSION_POSTURE_ENTERPRISE, transport="stdio", oauth_enabled=True
        )
    message = str(excinfo.value)
    assert "--transport=stdio" in message
    assert SESSION_POSTURE_WORKSTATION in message


@pytest.mark.parametrize("transport", NETWORK_TRANSPORTS)
def test_enterprise_without_oauth_is_refused(transport):
    """Unattended and unauthenticated: every connecting client gets every tool."""
    with pytest.raises(PostureConfigError) as excinfo:
        validate_posture(
            SESSION_POSTURE_ENTERPRISE, transport=transport, oauth_enabled=False
        )
    message = str(excinfo.value)
    assert "OAuth is not active" in message
    assert "--oauth-jwks-uri" in message


def test_enterprise_with_confirmation_required_tools_is_refused():
    """The failure this module exists for: a gate that is not a gate.

    An elicitation prompt reaches the client, and the client in an unattended
    deployment is the model that asked for the tool. Accepting this
    configuration would let an operator believe writes are gated when nothing
    stands in front of them.
    """
    with pytest.raises(PostureConfigError) as excinfo:
        validate_posture(
            SESSION_POSTURE_ENTERPRISE,
            transport="http",
            oauth_enabled=True,
            confirmation_required_tools={
                "upsert_document_by_id",
                "delete_document_by_id",
            },
        )
    message = str(excinfo.value)
    # Named, so the operator does not have to work out which ones.
    assert "delete_document_by_id" in message
    assert "upsert_document_by_id" in message
    assert "--disabled-tools" in message


def test_enterprise_accepts_an_empty_confirmation_set():
    """The resolved set is empty when the operator named nothing, which is fine."""
    assert (
        validate_posture(
            SESSION_POSTURE_ENTERPRISE,
            transport="http",
            oauth_enabled=True,
            confirmation_required_tools=set(),
        )
        == SESSION_POSTURE_ENTERPRISE
    )


# --- the posture set itself -----------------------------------------------------


@pytest.mark.parametrize("posture", ALLOWED_SESSION_POSTURES)
def test_every_allowed_posture_has_a_configuration_that_starts(posture):
    """A posture nothing can satisfy would be a flag that only ever refuses."""
    assert validate_posture(posture, **COHERENT[posture]) == posture


def test_an_unhandled_posture_is_refused_rather_than_guessed():
    """Unreachable through the CLI, which is exactly why it is asserted here.

    A third posture added to ``ALLOWED_SESSION_POSTURES`` and not handled must not fall
    into whichever branch happens to be last.
    """
    with pytest.raises(PostureConfigError) as excinfo:
        validate_posture("datacenter", transport="http", oauth_enabled=True)
    assert "Unknown deployment posture" in str(excinfo.value)


def test_posture_names_are_operator_facing():
    """These appear in configs, containers and runbooks; renaming one is breaking."""
    assert SESSION_POSTURE_WORKSTATION == "workstation"
    assert SESSION_POSTURE_ENTERPRISE == "enterprise"
    assert ALLOWED_SESSION_POSTURES == ("workstation", "enterprise")


# --- the wiring -----------------------------------------------------------------


def _host_settings(argv: list[str]) -> dict:
    """The real settings dict, as the CLI builds it for ``argv``.

    Same shape as ``tests/unit/test_settings_classification.py``: drive the
    command with ``run_app`` patched out, then run the lifespan far enough to
    read what reached ``AppContext``.
    """
    captured: dict = {}

    def capture(*_args, **kwargs):
        captured["lifespan"] = kwargs.get("lifespan")
        return MagicMock()

    with (
        patch("cb_mcp.core.app.FastMCP", side_effect=capture),
        patch("mcp_server.run_app"),
    ):
        result = CliRunner().invoke(mcp_server.main, argv, catch_exceptions=False)
    assert result.exit_code == 0, result.output

    out: dict = {}

    async def drive():
        async with captured["lifespan"](MagicMock()) as ctx:
            out.update(ctx.settings)

    asyncio.run(drive())
    return out


def test_settings_report_no_posture_when_none_is_declared():
    """Existing deployments keep their exact behaviour, and say so in diagnostics."""
    assert _host_settings([])["session_posture"] is None


def test_settings_report_the_declared_posture():
    assert (
        _host_settings(["--session-posture", SESSION_POSTURE_WORKSTATION])[
            "session_posture"
        ]
        == SESSION_POSTURE_WORKSTATION
    )


def test_an_incoherent_declaration_stops_the_server_starting():
    """The validation is reached from ``_start_server``, not merely importable.

    Default transport is stdio, so ``enterprise`` here is the contradiction.
    ``run_app`` is patched so a passing start would be visible as exit code 0
    rather than as a hung process.
    """
    with patch("mcp_server.run_app") as run_app:
        result = CliRunner().invoke(
            mcp_server.main, ["--session-posture", SESSION_POSTURE_ENTERPRISE]
        )
    assert result.exit_code != 0
    assert "--transport=stdio" in result.output
    run_app.assert_not_called()


def test_an_unknown_posture_is_rejected_by_the_command_line():
    """``click.Choice`` is the first gate; the module's own check is the second."""
    result = CliRunner().invoke(mcp_server.main, ["--session-posture", "datacenter"])
    assert result.exit_code != 0


def test_the_flag_is_documented_in_help():
    """An undocumented posture flag is one nobody declares."""
    result = CliRunner().invoke(mcp_server.main, ["operational", "--help"])
    assert result.exit_code == 0
    assert "--session-posture" in result.output
