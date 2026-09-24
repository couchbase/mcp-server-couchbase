"""Couchbase MCP servers — the command line that starts one.

A *server* here is one coherent set of tools over one backing service,
declared as a ``ServerSpec`` (see ``cb_mcp.core.spec``). This file is the
*host*: it owns the command line, resolves flags and environment into
configuration, and hands that configuration to the shared machinery in
``cb_mcp``. Nothing in that shared machinery reads a CLI flag or an
environment variable — that rule is what lets the same tools run inside
managed deployments that have no command line at all (CONTRIBUTING.md,
"Host-agnostic design"). The one narrow, named exception is
``cb_mcp.utils.cli_params``, which does the mechanical translation from
parsed Click params to configuration and says so in its own docstring.

Two servers ship today, one Click subcommand each:

  operational           the Couchbase cluster server. Also the *default*
                         subcommand, so a bare ``couchbase-mcp-server`` — which
                         is every published container's ENTRYPOINT and every
                         existing user config — still starts it.
  operational-insights  the Operational Insights server.

Both subcommands have the same shape, because everything a server can differ
in is a parameter:

  1. ``@server_options(...)`` declares its entire flag surface — six shared
     option stacks in one canonical order, defined once, not per server.
  2. The body imports its spec and provider *lazily*. This is deliberate: a
     process must load only the SDK of the server it is actually running.
  3. ``_start_server`` does everything else, identically for both.

To add a third server: write its ``ServerSpec`` and a provider satisfying
``cb_mcp.core.contracts.ProviderLifecycle`` (plus whatever service-specific
members its own tools need — see ``ClusterProvider`` and
``OperationalInsightsProvider`` for the two shapes that exist),
add a ``CredentialProfile`` next to the others in ``cb_mcp.utils.cli_params`` if
its credentials differ from the cluster ones, then copy either subcommand
below and change the five per-server facts — command name, credentials,
default port, default log file, and the spec/provider pair it imports.
``_start_server`` should not need to change; if it does, the thing you are
adding is probably not a server.
"""

from collections.abc import Callable, Mapping
from typing import Any

import click

from cb_mcp.core.app import build_app, run_app
from cb_mcp.core.cli import DefaultGroup
from cb_mcp.core.contracts import ProviderLifecycle
from cb_mcp.core.spec import ServerSpec
from cb_mcp.servers.operational.constants import (
    DEFAULT_OPERATIONAL_LOG_FILE,
    DEFAULT_OPERATIONAL_PORT,
)
from cb_mcp.servers.operational_insights.constants import (
    DEFAULT_OI_LOG_FILE,
    DEFAULT_OI_PORT,
)
from cb_mcp.utils.cli_params import (
    CLUSTER_CREDENTIALS,
    INSIGHTS_CREDENTIALS,
    CliParams,
    CredentialProfile,
    build_settings,
    gate_tools,
    resolved_logging_snapshot,
    server_options,
)

# --- Starting a server -------------------------------------------------------


def _start_server(
    spec: ServerSpec,
    params: Mapping[str, Any],
    *,
    credentials: CredentialProfile,
    provider_factory: Callable[[Mapping[str, Any]], ProviderLifecycle],
) -> None:
    """Run one server, from parsed flags to a listening process.

    Every step below is the same for every server; ``credentials`` and
    ``provider_factory`` are the only per-server inputs. ``provider_factory``
    receives the finished ``settings``, so the provider sees exactly what the
    diagnostic record reports.
    """
    cli = CliParams.from_click(params, credentials=credentials)
    # First: everything after this is logged.
    cli.logging.apply(sdk_log_hook=spec.sdk_log_hook)
    auth = cli.resolve_auth(spec)
    gated = gate_tools(spec, cli.gating, enforce_scopes=auth is not None)
    settings = build_settings(cli, gated=gated, oauth_enabled=auth is not None)

    # CLI-resolved configuration lives on AppContext, not in a module global,
    # so FastMCP's threadpool workers can read it through the request context.
    mcp = build_app(
        spec,
        tools=gated.tools,
        settings=settings,
        # A factory, not an instance: constructing the provider is deferred
        # to lifespan startup so nothing connects during tool discovery.
        provider_factory=lambda: provider_factory(settings),
        auth=auth,
        read_only_mode=cli.gating.read_only_mode,
        logging_config=resolved_logging_snapshot(),
    )

    run_app(
        mcp,
        transport=cli.transport.transport,
        host=cli.transport.host,
        port=cli.transport.port,
    )


# --- The command line ---------------------------------------------------------


@click.group(
    cls=DefaultGroup,
    default_cmd="operational",
    # Inherited by every subcommand context, so per-server --help keeps
    # showing "[default: ...]" without repeating this on each command.
    context_settings={"show_default": True},
)
@click.version_option(
    package_name="couchbase-mcp-server",
    prog_name="couchbase-mcp-server",
)
def main() -> None:
    """Couchbase MCP servers.

    Invoked without a subcommand, runs the operational server — the
    long-standing behaviour that existing configs and containers rely on.
    """


@main.command("operational", short_help="Operational cluster server (default).")
@server_options(
    credentials=CLUSTER_CREDENTIALS,
    default_port=DEFAULT_OPERATIONAL_PORT,
    default_log_file=DEFAULT_OPERATIONAL_LOG_FILE,
)
# Also on the subcommand so `couchbase-mcp-server operational --version` works.
@click.version_option(
    package_name="couchbase-mcp-server",
    prog_name="couchbase-mcp-server operational",
)
def operational(**params: Any) -> None:
    """Run the operational Couchbase cluster MCP server."""
    # Deliberately lazy: a process must only load the SDK of the server it
    # is actually running (see CONTRIBUTING.md's "Adding a new MCP server").
    from cb_mcp.servers.operational.spec import SPEC  # noqa: PLC0415
    from providers.operational import OperationalClusterProvider  # noqa: PLC0415

    _start_server(
        SPEC,
        params,
        credentials=CLUSTER_CREDENTIALS,
        provider_factory=lambda settings: OperationalClusterProvider(settings=settings),
    )


@main.command(
    "operational-insights",
    short_help="Operational Insights server.",
)
@server_options(
    credentials=INSIGHTS_CREDENTIALS,
    default_port=DEFAULT_OI_PORT,
    default_log_file=DEFAULT_OI_LOG_FILE,
)
# Also on the subcommand so `couchbase-mcp-server operational-insights --version` works.
@click.version_option(
    package_name="couchbase-mcp-server",
    prog_name="couchbase-mcp-server operational-insights",
)
def operational_insights(**params: Any) -> None:
    """Run the Couchbase Operational Insights MCP server."""
    # Deliberately lazy: a process must only load the SDK of the server it
    # is actually running (see CONTRIBUTING.md's "Adding a new MCP server").
    from cb_mcp.servers.operational_insights.spec import SPEC  # noqa: PLC0415
    from providers.operational_insights import (  # noqa: PLC0415
        OperationalInsightsClusterProvider,
    )

    _start_server(
        SPEC,
        params,
        credentials=INSIGHTS_CREDENTIALS,
        provider_factory=lambda settings: OperationalInsightsClusterProvider(
            settings=settings
        ),
    )


if __name__ == "__main__":
    main()
