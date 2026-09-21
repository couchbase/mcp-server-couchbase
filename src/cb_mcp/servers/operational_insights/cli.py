"""CLI option stack specific to the Operational Insights server.

Lives here rather than ``core/cli/options.py``: the shared ``credential_options``
in that module is Couchbase-specific (a ``couchbase://`` connection string,
mTLS client cert/key) and its own docstring already says a server backed by
a different service should define its own. This module imports only
``click`` and the shared ``compose`` helper, so it stays free of the
``couchbase_operational_insights`` SDK import and can be imported at CLI
module scope.
"""

import click

from ...core.cli.options import compose

#: Options are optional (no ``required=True``): the server must be able to
#: start in "lazy" mode for --help and tool discovery, matching the
#: operational server's credential_options. connect_to_operational_insights_cluster
#: validates presence at actual connection time instead.
#:
#: Env var names are CB_OI_*, not CB_*: CB_CONNECTION_STRING/CB_USERNAME/
#: CB_PASSWORD already mean the *operational* cluster and both subcommands
#: can run from the same shell/.env, so reusing them here would silently
#: feed a couchbase:// string to this server.
oi_credential_options = compose(
    click.option(
        "--connection-string",
        "connection_string",
        envvar="CB_OI_CONNECTION_STRING",
        help=(
            "Operational Insights endpoint URL, e.g. http://localhost:8095 "
            "(local server) or https://<host>:18095 (Capella). "
            "This is an HTTP(S) URL, not a couchbase:// connection string."
        ),
    ),
    click.option(
        "--username",
        "username",
        envvar="CB_OI_USERNAME",
        help="Operational Insights username",
    ),
    click.option(
        "--password",
        "password",
        envvar="CB_OI_PASSWORD",
        help="Operational Insights password",
    ),
)
