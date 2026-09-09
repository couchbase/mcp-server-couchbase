"""Regression matrix for the default-subcommand CLI routing.

Turning the CLI into a group is the one refactor that can silently break every
existing deployment: containers run the entrypoint with no arguments, CI starts
the server with none, and user configs pass only options. Each test below pins
one invocation form that worked before the group existed and must keep working.

Two failure modes these guard against specifically, both of which a naive
``resolve_command`` override exhibits:

* ``couchbase-mcp-server`` with no arguments exiting non-zero.
* ``couchbase-mcp-server --transport http`` (options, no subcommand) being
  rejected as an unknown option.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import click
import couchbase
import pytest
from click.shell_completion import ShellComplete
from click.testing import CliRunner

import mcp_server
from cb_mcp.core.cli import DefaultGroup


@pytest.fixture(autouse=True)
def mock_sdk_configure_logging():
    """The Couchbase SDK's configure_logging is one-shot per process."""
    with patch.object(couchbase, "configure_logging"):
        yield


def _invoke(args: list[str], env: dict[str, str] | None = None):
    """Run the CLI with the server assembled but never started."""
    captured: dict = {}

    def capture(*_args, **kwargs):
        captured["lifespan"] = kwargs.get("lifespan")
        return MagicMock()

    with (
        patch("cb_mcp.core.app.FastMCP", side_effect=capture),
        # Patched where it is *used*: mcp_server imported the name, so it
        # holds its own binding and patching cb_mcp.core.app would not take.
        patch("mcp_server.run_app") as run,
    ):
        result = CliRunner().invoke(
            mcp_server.main, args, env=env, catch_exceptions=False
        )
    return result, captured.get("lifespan"), run


def _settings_from(lifespan) -> dict:
    """Drive the lifespan far enough to read the resolved settings."""
    out: dict = {}

    async def drive():
        async with lifespan(MagicMock()) as app_context:
            out.update(app_context.settings)

    asyncio.run(drive())
    return out


class TestBareInvocationStillWorks:
    """The forms that predate the group and must never regress."""

    def test_no_arguments_runs_the_operational_server(self):
        """The single most important test in this file.

        The Docker image's ENTRYPOINT takes no CMD and every CI job starts the
        server with environment variables only, so a non-zero exit here breaks
        all published containers at once.
        """
        result, lifespan, run = _invoke([])
        assert result.exit_code == 0, result.output
        assert lifespan is not None, "the server was never assembled"
        run.assert_called_once()

    def test_options_without_a_subcommand(self):
        """Options with no subcommand must reach the operational server."""
        result, lifespan, _ = _invoke(["--transport", "http"])
        assert result.exit_code == 0, result.output
        assert _settings_from(lifespan)["transport"] == "http"

    def test_env_vars_only(self):
        result, lifespan, _ = _invoke(
            [], env={"CB_CONNECTION_STRING": "couchbase://from-env"}
        )
        assert result.exit_code == 0, result.output
        assert _settings_from(lifespan)["connection_string"] == "couchbase://from-env"

    def test_option_value_equal_to_a_subcommand_name(self):
        """``--log-file operational`` is a value, not a command.

        Injection keys off the first token, which is the option itself, so the
        value is consumed by the subcommand's parser as normal.
        """
        result, lifespan, _ = _invoke(["--log-file", "operational"])
        assert result.exit_code == 0, result.output
        assert _settings_from(lifespan) is not None


class TestExplicitSubcommand:
    def test_explicit_operational(self):
        result, lifespan, _ = _invoke(["operational", "--transport", "http"])
        assert result.exit_code == 0, result.output
        assert _settings_from(lifespan)["transport"] == "http"

    def test_version_on_group_and_subcommand(self):
        """--version must work at both levels, and report the console-script
        name at both — click derives it from the root context's info_name."""
        for args in (["--version"], ["operational", "--version"]):
            result = CliRunner().invoke(
                mcp_server.main, args, prog_name="couchbase-mcp-server"
            )
            assert result.exit_code == 0, result.output
            assert "couchbase-mcp-server, version" in result.output


class TestFailsLoudlyRatherThanSilently:
    """Ambiguous input must error, never quietly run the wrong thing."""

    def test_subcommand_after_options_is_rejected(self):
        """``--transport http operational`` must not silently misroute.

        The group's parser stops at the first non-option, so a subcommand
        placed after options would otherwise be swallowed as a stray argument
        while its own options fell back to defaults.
        """
        result = CliRunner().invoke(mcp_server.main, ["--transport", "http", "bogus"])
        assert result.exit_code == 2
        assert "unexpected extra argument" in result.output.lower()

    def test_unknown_option_still_errors(self):
        result = CliRunner().invoke(mcp_server.main, ["--definitely-not-an-option"])
        assert result.exit_code == 2
        assert "no such option" in result.output.lower()

    def test_option_missing_its_value_still_errors(self):
        result = CliRunner().invoke(mcp_server.main, ["--transport"])
        assert result.exit_code == 2
        assert "requires an argument" in result.output.lower()


class TestHelpSurface:
    def test_bare_help_documents_the_default_servers_options(self):
        """Without the format_options merge the group would advertise almost
        nothing, since the ~35 options live on the subcommand."""
        result = CliRunner().invoke(mcp_server.main, ["--help"])
        assert result.exit_code == 0
        for flag in ("--connection-string", "--log-level", "--transport"):
            assert flag in result.output
        assert "[default:" in result.output
        assert "Commands:" in result.output

    def test_subcommand_help_inherits_show_default(self):
        """show_default is set on the group context and must propagate down."""
        result = CliRunner().invoke(mcp_server.main, ["operational", "--help"])
        assert result.exit_code == 0
        assert "[default:" in result.output


class TestDefaultGroupUnit:
    """Behaviour of the group itself, independent of this CLI's options."""

    @staticmethod
    def _group() -> DefaultGroup:
        group = DefaultGroup(name="g", default_cmd="alpha")

        @group.command()
        def alpha():
            click.echo("ALPHA")

        @group.command()
        def beta():
            click.echo("BETA")

        return group

    def test_completion_lists_every_subcommand(self):
        """Injection must be skipped under resilient parsing.

        Shell completion re-parses with that flag set; injecting there would
        hide every sibling command from the user's shell.
        """
        group = self._group()
        completions = ShellComplete(group, {}, "g", "_G").get_completions([], "")
        assert {c.value for c in completions} == {"alpha", "beta"}

    def test_known_subcommand_is_not_redirected(self):
        result = CliRunner().invoke(self._group(), ["beta"])
        assert result.exit_code == 0
        assert "BETA" in result.output

    def test_bare_invocation_reaches_the_default(self):
        result = CliRunner().invoke(self._group(), [])
        assert result.exit_code == 0
        assert "ALPHA" in result.output

    def test_group_without_a_default_behaves_normally(self):
        group = DefaultGroup(name="g", default_cmd=None)

        @group.command()
        def alpha():
            click.echo("ALPHA")

        result = CliRunner().invoke(group, [])
        assert result.exit_code != 0 or "ALPHA" not in result.output
