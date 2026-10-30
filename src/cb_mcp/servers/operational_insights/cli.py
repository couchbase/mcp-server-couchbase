"""CLI option stack specific to the Operational Insights server.

Lives here rather than ``core/cli/options.py``: the shared ``credential_options``
in that module is Couchbase-specific (a ``couchbase://`` connection string,
mTLS client cert/key) and its own docstring already says a server backed by
a different service should define its own. This module imports only
``click``, the standard library and SDK-free ``cb_mcp`` modules, so it stays
free of the ``couchbase_operational_insights`` SDK import and can be imported
at CLI module scope.
"""

import logging
from collections.abc import Mapping
from typing import Any

import click

from ...core.cli.options import compose
from .constants import (
    DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT,
    MAX_ASYNC_QUERY_RESULT_TIMEOUT,
    OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE,
)

logger = logging.getLogger(f"{OPERATIONAL_INSIGHTS_LOGGER_NAMESPACE}.cli")

OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT_KEY = (
    "operational_insights_async_query_result_timeout"
)

#: Options are optional (no ``required=True``): the server must be able to
#: start in "lazy" mode for --help and tool discovery, matching the
#: operational server's credential_options. connect_to_operational_insights_cluster
#: validates presence at actual connection time instead.
#:
#: Env var names are CB_OPERATIONAL_INSIGHTS_*, not CB_*: CB_CONNECTION_STRING/CB_USERNAME/
#: CB_PASSWORD already mean the *operational* cluster and both subcommands
#: can run from the same shell/.env, so reusing them here would silently
#: feed a couchbase:// string to this server.
operational_insights_credential_options = compose(
    click.option(
        "--connection-string",
        "connection_string",
        envvar="CB_OPERATIONAL_INSIGHTS_CONNECTION_STRING",
        help=(
            "Operational Insights endpoint URL, e.g. http://localhost:8095 "
            "(local server) or https://<host>:18095 (Capella). "
            "This is an HTTP(S) URL, not a couchbase:// connection string."
        ),
    ),
    click.option(
        "--username",
        "username",
        envvar="CB_OPERATIONAL_INSIGHTS_USERNAME",
        help="Operational Insights username",
    ),
    click.option(
        "--password",
        "password",
        envvar="CB_OPERATIONAL_INSIGHTS_PASSWORD",
        help="Operational Insights password",
    ),
    click.option(
        "--ca-cert-path",
        "ca_cert_path",
        envvar="CB_OPERATIONAL_INSIGHTS_CA_CERT_PATH",
        help="Path to the server trust store (CA certificate) file. The certificate at this path is used to verify the server certificate during the authentication process.",
    ),
    click.option(
        "--client-cert-path",
        "client_cert_path",
        envvar="CB_OPERATIONAL_INSIGHTS_CLIENT_CERT_PATH",
        help="Path to the client certificate used for mTLS authentication. "
        "Either a PEM certificate (paired with --client-key-path) or a "
        "PKCS#12 bundle (.p12/.pfx, --client-key-path left unset). Requires "
        "an https:// --connection-string. When set, --username/--password "
        "are ignored.",
    ),
    click.option(
        "--client-key-path",
        "client_key_path",
        envvar="CB_OPERATIONAL_INSIGHTS_CLIENT_KEY_PATH",
        help="Path to the client certificate's private key file (PEM). Leave "
        "unset when --client-cert-path is a PKCS#12 bundle that already "
        "contains the key.",
    ),
    click.option(
        "--client-cert-password",
        "client_cert_password",
        envvar="CB_OPERATIONAL_INSIGHTS_CLIENT_CERT_PASSWORD",
        help="Decryption password for the client key/PKCS#12 bundle, if it "
        "is encrypted. Omit if the file is unencrypted.",
    ),
    click.option(
        "--operational-insights-async-query-result-timeout",
        "operational_insights_async_query_result_timeout",
        envvar="CB_MCP_OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT",
        # A string, not IntRange: a bad or too-large value is warned about
        # and replaced in resolve_async_query_result_timeout rather than
        # exiting 2 during argument parsing, which would take a container
        # deployment down over one setting.
        type=str,
        default=None,
        help="Timeout, in seconds, for each async query handle request: "
        "starting the query, checking its status, fetching, discarding and "
        "cancelling. For a fetch it bounds the whole download, so a result "
        "that takes longer than this to read fails. Must be a positive "
        f"integer; defaults to {DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT}. Values "
        f"above {MAX_ASYNC_QUERY_RESULT_TIMEOUT} are clamped to "
        f"{MAX_ASYNC_QUERY_RESULT_TIMEOUT} with a warning; any other invalid "
        "value is logged as a warning and the default is used.",
    ),
)


def resolve_async_query_result_timeout(value: str | None, *, warn: bool) -> int:
    """The async query result timeout to use, in seconds.

    ``None`` (flag and environment variable unset) is the default, silently.
    Anything that is not a positive integer — ``0``, ``-5``, ``1.5``, ``abc``
    — falls back to the default. A value above
    ``MAX_ASYNC_QUERY_RESULT_TIMEOUT`` is clamped to it: the operator asked
    for a long timeout, so the maximum is closer to that than the default.

    ``warn=False`` resolves silently, for the settings build, which runs
    before logging is configured: a warning emitted then bypasses the
    configured sinks. ``warn_about_operational_insights_settings`` re-runs this
    with ``warn=True`` once the handlers exist.
    """
    if value is None:
        return DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT
    try:
        seconds = int(str(value).strip())
    except ValueError:
        seconds = 0
    if seconds < 1:
        if warn:
            logger.warning(
                f"CB_MCP_OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT {value!r} "
                "is not a positive integer; using the default "
                f"{DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT} seconds."
            )
        return DEFAULT_ASYNC_QUERY_RESULT_TIMEOUT
    if seconds > MAX_ASYNC_QUERY_RESULT_TIMEOUT:
        if warn:
            logger.warning(
                f"CB_MCP_OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT {seconds} "
                f"exceeds the maximum {MAX_ASYNC_QUERY_RESULT_TIMEOUT} seconds; "
                f"clamping to {MAX_ASYNC_QUERY_RESULT_TIMEOUT}."
            )
        return MAX_ASYNC_QUERY_RESULT_TIMEOUT
    return seconds


def resolve_operational_insights_settings(
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """The credential settings slice with the timeout replaced by its effective value."""
    return {
        **settings,
        OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT_KEY: resolve_async_query_result_timeout(
            settings[OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT_KEY], warn=False
        ),
    }


def warn_about_operational_insights_settings(settings: Mapping[str, Any]) -> None:
    """Log the warnings ``resolve_operational_insights_settings`` held back.

    Takes the raw settings slice. Call after ``LoggingParams.apply``.
    """
    resolve_async_query_result_timeout(
        settings[OPERATIONAL_INSIGHTS_ASYNC_QUERY_RESULT_TIMEOUT_KEY], warn=True
    )
