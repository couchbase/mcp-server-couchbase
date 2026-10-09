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
its credentials differ from the cluster ones, add a loader for its
spec/provider pair to ``_SERVERS``, then copy either subcommand below and
change the four per-server facts — command name, credentials, default port and
default log file. ``_build_server`` should not need to change; if it does, the
thing you are adding is probably not a server.

``--workers N`` (N > 1) serves one server from N processes. The CLI process
validates the whole configuration, then becomes a Uvicorn supervisor that
spawns the workers; each worker imports ``create_app`` below, which rebuilds
the same server from the parsed flags the supervisor hands it. ``_SERVERS``
is keyed by subcommand name so a worker can find its server without the
command line.
"""

import os
import socket
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

import click
import fastmcp
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan

from cb_mcp.core.app import build_app, run_app
from cb_mcp.core.cli import DefaultGroup
from cb_mcp.core.contracts import ProviderLifecycle
from cb_mcp.core.serving import (
    ServingConfig,
    ServingConfigError,
    resolve_serving,
    uvicorn_log_level,
    worker_log_file,
)
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
    decode_worker_config,
    encode_worker_config,
    gate_tools,
    resolve_deployment_for,
    resolved_logging_snapshot,
    server_options,
)
from cb_mcp.utils.telemetry import send_install_ping

#: Environment variable carrying the supervisor's parsed flags to each worker.
#: Underscore-prefixed and undocumented: an internal handoff channel, not a
#: configuration surface.
WORKER_CONFIG_ENV_VAR = "_CB_MCP_WORKER_CONFIG"

#: What Uvicorn imports in each worker. Must name ``create_app`` below.
WORKER_APP_IMPORT_STRING = "mcp_server:create_app"


# --- The servers ----------------------------------------------------------------


class _Server(NamedTuple):
    """One server's per-run inputs: what it is, whose credentials, how it connects.

    ``provider_factory`` receives the finished ``settings``, so the provider
    sees exactly what the diagnostic record reports.
    """

    spec: ServerSpec
    credentials: CredentialProfile
    provider_factory: Callable[[Mapping[str, Any]], ProviderLifecycle]


# The loaders import *inside* the function on purpose: a process must only
# load the SDK of the server it is actually running (see CONTRIBUTING.md's
# "Adding a new MCP server"), and tests/unit/test_sdk_isolation.py checks it.


def _load_operational() -> _Server:
    from cb_mcp.servers.operational.spec import SPEC  # noqa: PLC0415
    from providers.operational import OperationalClusterProvider  # noqa: PLC0415

    return _Server(
        SPEC,
        CLUSTER_CREDENTIALS,
        lambda settings: OperationalClusterProvider(settings=settings),
    )


def _load_operational_insights() -> _Server:
    from cb_mcp.servers.operational_insights.spec import SPEC  # noqa: PLC0415
    from providers.operational_insights import (  # noqa: PLC0415
        OperationalInsightsClusterProvider,
    )

    return _Server(
        SPEC,
        INSIGHTS_CREDENTIALS,
        lambda settings: OperationalInsightsClusterProvider(settings=settings),
    )


#: Keyed by subcommand name, which is also what a worker is told to rebuild.
_SERVERS: dict[str, Callable[[], _Server]] = {
    "operational": _load_operational,
    "operational-insights": _load_operational_insights,
}


# --- Starting a server -------------------------------------------------------


class _BuiltServer(NamedTuple):
    mcp: FastMCP
    cli: CliParams
    spec: ServerSpec
    serving: ServingConfig


def _build_server(
    server_id: str,
    params: Mapping[str, Any],
    *,
    send_startup_ping: bool = True,
) -> _BuiltServer:
    """Build one server from parsed flags, ready to run but not running.

    Every step is the same for every server and for every process: the
    single-process CLI, a ``--workers`` supervisor validating before it
    spawns, and each worker rebuilding from the supervisor's flags all come
    through here, so they cannot drift apart.
    """
    server = _SERVERS[server_id]()
    spec = server.spec
    cli = CliParams.from_click(params, credentials=server.credentials)
    # First: everything after this is logged.
    cli.logging.apply(sdk_log_hook=spec.sdk_log_hook)
    auth = cli.resolve_auth(spec)
    # Resolved from the credentials this run was given, so a tool that only
    # works on one kind of cluster is never registered against the other.
    deployment = resolve_deployment_for(spec, cli.credentials)
    gated = gate_tools(
        spec,
        cli.gating,
        enforce_scopes=auth is not None,
        deployment=deployment,
    )
    # After gating: the confirmation check needs the resolved tool names.
    try:
        serving = resolve_serving(
            server_id=spec.id,
            transport=cli.transport.transport,
            workers=cli.transport.workers,
            stateless_http=cli.transport.stateless_http,
            thread_pool_size=cli.transport.thread_pool_size,
            supports_multiple_workers=spec.supports_multiple_workers,
            confirmation_required=gated.confirmation_required,
            # What FastMCP would do if left alone (FASTMCP_STATELESS_HTTP),
            # so a deployment relying on it keeps working and the checks see
            # the mode that will actually run.
            runtime_default_stateless=fastmcp.settings.stateless_http,
        )
    except ServingConfigError as e:
        raise click.UsageError(str(e)) from e
    settings = build_settings(
        cli, gated=gated, oauth_enabled=auth is not None, serving=serving
    )

    # CLI-resolved configuration lives on AppContext, not in a module global,
    # so FastMCP's threadpool workers can read it through the request context.
    mcp = build_app(
        spec,
        tools=gated.tools,
        settings=settings,
        # A factory, not an instance: constructing the provider is deferred
        # to lifespan startup so nothing connects during tool discovery.
        provider_factory=lambda: server.provider_factory(settings),
        auth=auth,
        read_only_mode=cli.gating.read_only_mode,
        logging_config=resolved_logging_snapshot(),
        thread_pool_size=serving.thread_pool_size,
        send_startup_ping=send_startup_ping,
    )
    return _BuiltServer(mcp, cli, spec, serving)


def _start_server(server_id: str, params: Mapping[str, Any]) -> None:
    """Run one server, from parsed flags to a listening process (or N)."""
    built = _build_server(server_id, params)
    transport = built.cli.transport

    if built.serving.workers > 1:
        # The server built above is only used to validate the configuration
        # here, once, instead of N workers spawning and failing in a loop.
        # One startup event for the deployment; workers are told not to send
        # their own, so N processes don't look like N installs.
        send_install_ping(transport.transport, server_id=built.spec.id)
        _run_workers(server_id, params, built)
        return

    run_app(
        built.mcp,
        transport=transport.transport,
        host=transport.host,
        port=transport.port,
        stateless_http=built.serving.stateless_http,
    )


def _run_workers(
    server_id: str, params: Mapping[str, Any], built: _BuiltServer
) -> None:
    """Serve streamable HTTP from ``workers`` processes under a Uvicorn supervisor.

    Uvicorn binds the socket once and spawns children that accept from it, so
    the kernel balances connections across them with no proxy in front. It
    also restarts children that die and forwards shutdown signals. Children
    are spawned, not forked, which is why the configuration travels through
    the environment.
    """
    transport = built.cli.transport
    os.environ[WORKER_CONFIG_ENV_VAR] = encode_worker_config(server_id, params)
    uvicorn.run(
        WORKER_APP_IMPORT_STRING,
        factory=True,
        host=transport.host,
        port=transport.port,
        workers=built.serving.workers,
        # Our AppContext (and with it the cluster connection) is created by
        # the app's lifespan, so it must run in every worker.
        lifespan="on",
        # Matches what FastMCP uses for its own single-process server.
        timeout_graceful_shutdown=2,
        log_level=uvicorn_log_level(built.cli.logging.level.level),
    )


def create_app() -> StarletteWithLifespan:
    """ASGI factory Uvicorn calls once inside each ``--workers`` process.

    Rebuilds the server the supervisor validated, from the flags it parsed,
    with logs written to files named for this host and process so workers
    never rotate a shared file. Not an entrypoint for an external ASGI
    server: without the supervisor's configuration this raises.
    """
    raw = os.environ.get(WORKER_CONFIG_ENV_VAR)
    if not raw:
        raise RuntimeError(
            f"{WORKER_CONFIG_ENV_VAR} is not set, so this worker has no "
            "configuration to start from. Start the server with "
            "'couchbase-mcp-server --transport http --workers N' instead of "
            "pointing an ASGI server at mcp_server:create_app."
        )
    server_id, params = decode_worker_config(raw)
    params["log_file"] = worker_log_file(
        params["log_file"], host=socket.gethostname(), pid=os.getpid()
    )
    built = _build_server(server_id, params, send_startup_ping=False)
    # The supervisor already resolved this configuration as stateless.
    return built.mcp.http_app(stateless_http=built.serving.stateless_http)


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
    _start_server("operational", params)


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
    _start_server("operational-insights", params)


if __name__ == "__main__":
    main()
