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
    click.option(
        "--ca-cert-path",
        "ca_cert_path",
        envvar="CB_OI_CA_CERT_PATH",
        help="Path to the server trust store (CA certificate) file. The certificate at this path is used to verify the server certificate during the authentication process.",
    ),
    click.option(
        "--client-cert-path",
        "client_cert_path",
        envvar="CB_OI_CLIENT_CERT_PATH",
        help="Path to the client certificate used for mTLS authentication. "
        "Either a PEM certificate (paired with --client-key-path) or a "
        "PKCS#12 bundle (.p12/.pfx, --client-key-path left unset). Requires "
        "an https:// --connection-string. When set, --username/--password "
        "are ignored.",
    ),
    click.option(
        "--client-key-path",
        "client_key_path",
        envvar="CB_OI_CLIENT_KEY_PATH",
        help="Path to the client certificate's private key file (PEM). Leave "
        "unset when --client-cert-path is a PKCS#12 bundle that already "
        "contains the key.",
    ),
    click.option(
        "--client-cert-password",
        "client_cert_password",
        envvar="CB_OI_CLIENT_CERT_PASSWORD",
        help="Decryption password for the client key/PKCS#12 bundle, if it "
        "is encrypted. Omit if the file is unencrypted.",
    ),
)
