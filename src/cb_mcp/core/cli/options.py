"""Reusable Click option stacks shared by every server this package hosts.

Each stack is a group of related flags that can be applied to a command as a
single decorator. Splitting them this way means a second server reuses the
transport, logging, tool-gating and OAuth flags verbatim and only has to
declare the credentials that differ, instead of copying ~35 decorators.

Option *order* here is load-bearing: it determines the order of ``--help``, and
the existing grouping is deliberately interleaved (``--read-only-mode`` sits
between the credentials and the transport flags, and the tool-gating flags sit
between ``--port`` and the logging flags). The stacks below preserve that exact
order rather than regrouping thematically, so ``--help`` is unchanged.
"""

from collections.abc import Callable

import click

from ...utils.cli import (
    validate_log_level,
    validate_log_path,
    validate_log_sinks,
    validate_scope_label,
)
from ...utils.constants import (
    ALLOWED_OAUTH_ALGORITHMS,
    ALLOWED_TRANSPORTS,
    DEFAULT_HOST,
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_LEVEL,
    DEFAULT_LOG_SINKS,
    DEFAULT_OAUTH_ALGORITHM,
    DEFAULT_PORT,
    DEFAULT_READ_ONLY_MODE,
    DEFAULT_TRANSPORT,
    SCOPE_READ,
    SCOPE_WRITE,
)


def compose(*decorators: Callable) -> Callable:
    """Combine decorators so they apply as if stacked in source order.

    ``@compose(a, b)`` is equivalent to ``@a`` written above ``@b``. The
    reversal is deliberate: Click appends each option to ``__click_params__``
    and ``Command`` reverses that list, so the *topmost* decorator becomes
    ``params[0]`` and therefore the first row of ``--help``.
    """

    def wrap(f: Callable) -> Callable:
        for decorator in reversed(decorators):
            f = decorator(f)
        return f

    return wrap


credential_options = compose(
    click.option(
        "--connection-string",
        "connection_string",
        envvar="CB_CONNECTION_STRING",
        help="Couchbase connection string (required for operations)",
    ),
    click.option(
        "--username",
        "username",
        envvar="CB_USERNAME",
        help="Couchbase database user (required for operations)",
    ),
    click.option(
        "--password",
        "password",
        envvar="CB_PASSWORD",
        help="Couchbase database password (required for operations)",
    ),
    click.option(
        "--ca-cert-path",
        "ca_cert_path",
        envvar="CB_CA_CERT_PATH",
        help="Path to the server trust store (CA certificate) file. The certificate at this path is used to verify the server certificate during the authentication process.",
    ),
    click.option(
        "--client-cert-path",
        "client_cert_path",
        envvar="CB_CLIENT_CERT_PATH",
        help="Path to the client certificate file used for mTLS authentication.",
    ),
    click.option(
        "--client-key-path",
        "client_key_path",
        envvar="CB_CLIENT_KEY_PATH",
        help="Path to the client certificate key file used for mTLS authentication.",
    ),
)
"""Couchbase cluster credentials and TLS material. Per-server: a server backed by a different service should define its own rather than reuse these."""

read_only_option = compose(
    click.option(
        "--read-only-mode",
        "read_only_mode",
        envvar="CB_MCP_READ_ONLY_MODE",
        type=bool,
        default=DEFAULT_READ_ONLY_MODE,
        help="Enable read-only mode. When True, all write operations (KV and Query) are disabled and KV write tools are not loaded. Set to False to enable write operations.",
    ),
)
"""Whether write tools are loaded at all. Kept separate from the other gating flags to preserve the historical --help ordering."""


def transport_options(*, default_port: int = DEFAULT_PORT) -> Callable:
    """Transport selection and the network bind address. Shared by every server.

    A factory because the default port is per-server: two servers left on one
    port cannot both bind. Parameterising here rather than resolving later
    keeps the correct value visible in each subcommand's ``--help``.
    """
    return compose(
        click.option(
            "--transport",
            "transport",
            envvar=["CB_MCP_TRANSPORT"],
            type=click.Choice(ALLOWED_TRANSPORTS),
            default=DEFAULT_TRANSPORT,
            help="Transport mode for the server (stdio, http or sse). Default is stdio. OAuth is only honored with http (streamable-http).",
        ),
        click.option(
            "--host",
            "host",
            envvar="CB_MCP_HOST",
            default=DEFAULT_HOST,
            help="Host to run the server on.",
        ),
        click.option(
            "--port",
            "port",
            envvar="CB_MCP_PORT",
            default=default_port,
            help="Port to run the server on.",
        ),
    )


tool_gating_options = compose(
    click.option(
        "--disabled-tools",
        "disabled_tools",
        envvar="CB_MCP_DISABLED_TOOLS",
        help="Tools to disable. Accepts comma-separated tool names (e.g., 'tool_1,tool_2') "
        "or a file path containing one tool name per line.",
    ),
    click.option(
        "--confirmation-required-tools",
        "confirmation_required_tools",
        envvar="CB_MCP_CONFIRMATION_REQUIRED_TOOLS",
        help="Comma-separated tool names that require user confirmation before execution. "
        "Also accepts a file path containing one tool name per line. "
        "Requires the MCP client to support elicitation.",
    ),
)
"""Per-tool opt-outs and confirmation requirements. Shared by every server."""


def logging_options(*, default_log_file: str = DEFAULT_LOG_FILE) -> Callable:
    """Log level, sinks, and per-level rotation/retention. Shared by every server.

    A factory because the default log file is per-server: two servers sharing
    one base path put two RotatingFileHandlers on the same files, and
    rotation is not multi-process safe.
    """
    return compose(
        click.option(
            "--log-level",
            "log_level",
            envvar="CB_MCP_LOG_LEVEL",
            default=DEFAULT_LOG_LEVEL,
            callback=validate_log_level,
            help="Logging level for MCP server and Couchbase SDK. Allowed values: "
            "off, debug, info, warning, error. Use 'off' to disable logging entirely. Invalid values fall "
            "back to the default with an error log entry.",
        ),
        click.option(
            "--log-sinks",
            "log_sinks",
            envvar="CB_MCP_LOG_SINKS",
            default=DEFAULT_LOG_SINKS,
            callback=validate_log_sinks,
            help="Comma-separated list of log sinks. Allowed values: stderr, file. "
            "Include 'file' (optionally with --log-file) to write per-level files; "
            "include 'stderr' to write to the console.",
        ),
        click.option(
            "--log-file",
            "log_file",
            envvar="CB_MCP_LOG_FILE",
            default=default_log_file,
            callback=validate_log_path,
            help="Base file path for the per-level log files. One rotating file is written "
            "per level, derived by inserting the level name: e.g. mcp_server.log -> "
            "mcp_server.debug.log, mcp_server.info.log, mcp_server.warning.log, "
            "mcp_server.error.log (the error file also captures CRITICAL). Only active "
            "when 'file' is in --log-sinks.",
        ),
        click.option(
            "--log-rotation-max-size-mb",
            "log_rotation_max_size_mb",
            envvar="CB_MCP_LOG_ROTATION_MAX_SIZE_MB",
            # Default None so the 1 MB default is applied only when neither this nor the
            # deprecated --log-max-bytes is set.
            type=click.FloatRange(min=0),
            default=None,
            help="Global maximum size in MB per-level log file before it rotates, "
            "inherited by every level unless overridden. Default is 1 MB. 0 is invalid "
            "and falls back to the default with a startup warning.",
        ),
        click.option(
            "--log-max-bytes",
            "log_max_bytes",
            envvar="CB_MCP_LOG_MAX_BYTES",
            # DEPRECATED: superseded by --log-rotation-max-size-mb (MB). Still honored in
            # bytes for backward compatibility. Default None so it's only applied when
            # explicitly set; if set alongside --log-rotation-max-size-mb it is ignored.
            type=click.IntRange(min=0),
            default=None,
            help="[DEPRECATED] Global rotation size in bytes; use --log-rotation-max-size-mb "
            "(MB) instead. Still honored for backward compatibility. Ignored when "
            "--log-rotation-max-size-mb is also set. 0 is invalid and falls back to the "
            "default with a startup warning.",
        ),
        click.option(
            "--log-error-rotation-max-size-mb",
            "log_error_rotation_max_size_mb",
            envvar="CB_MCP_LOG_ERROR_ROTATION_MAX_SIZE_MB",
            type=click.FloatRange(min=0),
            default=None,
            help="Rotation size in MB for the ERROR log file. Overrides "
            "--log-rotation-max-size-mb for ERROR; inherits it when unset. 0 is invalid and "
            "falls back to the inherited global with a startup warning.",
        ),
        click.option(
            "--log-warning-rotation-max-size-mb",
            "log_warning_rotation_max_size_mb",
            envvar="CB_MCP_LOG_WARNING_ROTATION_MAX_SIZE_MB",
            type=click.FloatRange(min=0),
            default=None,
            help="Rotation size in MB for the WARNING log file. Overrides "
            "--log-rotation-max-size-mb for WARNING; inherits it when unset. 0 is invalid "
            "and falls back to the inherited global with a startup warning.",
        ),
        click.option(
            "--log-info-rotation-max-size-mb",
            "log_info_rotation_max_size_mb",
            envvar="CB_MCP_LOG_INFO_ROTATION_MAX_SIZE_MB",
            type=click.FloatRange(min=0),
            default=None,
            help="Rotation size in MB for the INFO log file. Overrides "
            "--log-rotation-max-size-mb for INFO; inherits it when unset. 0 is invalid and "
            "falls back to the inherited global with a startup warning.",
        ),
        click.option(
            "--log-debug-rotation-max-size-mb",
            "log_debug_rotation_max_size_mb",
            envvar="CB_MCP_LOG_DEBUG_ROTATION_MAX_SIZE_MB",
            type=click.FloatRange(min=0),
            default=None,
            help="Rotation size in MB for the DEBUG log file. Overrides "
            "--log-rotation-max-size-mb for DEBUG; inherits it when unset. 0 is invalid and "
            "falls back to the inherited global with a startup warning.",
        ),
        click.option(
            "--log-retention-backup-count",
            "log_retention_backup_count",
            envvar="CB_MCP_LOG_RETENTION_BACKUP_COUNT",
            # 0 keeps no rotated backups (only the live file); negative is rejected.
            type=click.IntRange(min=0),
            default=DEFAULT_LOG_BACKUP_COUNT,
            help="Number of rotated backup files kept per-level log file, excluding "
            "the live file. Applies to every level unless overridden per level. Set to 0 "
            "to keep only the live file.",
        ),
        click.option(
            "--log-error-retention-backup-count",
            "log_error_retention_backup_count",
            envvar="CB_MCP_LOG_ERROR_RETENTION_BACKUP_COUNT",
            type=click.IntRange(min=0),
            default=None,
            help="Rotated backups kept for the ERROR log file. Overrides "
            "--log-retention-backup-count for ERROR; inherits it when unset.",
        ),
        click.option(
            "--log-warning-retention-backup-count",
            "log_warning_retention_backup_count",
            envvar="CB_MCP_LOG_WARNING_RETENTION_BACKUP_COUNT",
            type=click.IntRange(min=0),
            default=None,
            help="Rotated backups kept for the WARNING log file. Overrides "
            "--log-retention-backup-count for WARNING; inherits it when unset.",
        ),
        click.option(
            "--log-info-retention-backup-count",
            "log_info_retention_backup_count",
            envvar="CB_MCP_LOG_INFO_RETENTION_BACKUP_COUNT",
            type=click.IntRange(min=0),
            default=None,
            help="Rotated backups kept for the INFO log file. Overrides "
            "--log-retention-backup-count for INFO; inherits it when unset.",
        ),
        click.option(
            "--log-debug-retention-backup-count",
            "log_debug_retention_backup_count",
            envvar="CB_MCP_LOG_DEBUG_RETENTION_BACKUP_COUNT",
            type=click.IntRange(min=0),
            default=None,
            help="Rotated backups kept for the DEBUG log file. Overrides "
            "--log-retention-backup-count for DEBUG; inherits it when unset.",
        ),
    )


oauth_options = compose(
    click.option(
        "--oauth-jwks-uri",
        "oauth_jwks_uri",
        envvar="CB_MCP_OAUTH_JWT_JWKS_URI",
        default=None,
        help="JWKS endpoint of the upstream identity provider, used to verify "
        "bearer JWT signatures (e.g. https://auth.example.com/.well-known/jwks.json). "
        "Required to enable OAuth (along with --oauth-issuer and --oauth-audience). "
        "Only honored when --transport=http.",
    ),
    click.option(
        "--oauth-issuer",
        "oauth_issuer",
        envvar="CB_MCP_OAUTH_JWT_ISSUER",
        default=None,
        help="Expected JWT 'iss' claim value. Also advertised as the authorization "
        "server in the protected-resource metadata when --oauth-mcp-base-url is set. "
        "Required to enable OAuth.",
    ),
    click.option(
        "--oauth-audience",
        "oauth_audience",
        envvar="CB_MCP_OAUTH_JWT_AUDIENCE",
        default=None,
        help="Expected JWT 'aud' claim value. Required to enable OAuth.",
    ),
    click.option(
        "--oauth-algorithm",
        "oauth_algorithm",
        envvar="CB_MCP_OAUTH_JWT_ALGORITHM",
        type=click.Choice(ALLOWED_OAUTH_ALGORITHMS),
        default=DEFAULT_OAUTH_ALGORITHM,
        show_default=True,
        help="JWT signing algorithm. One of RS256/384/512, ES256/384/512, PS256/384/512.",
    ),
    click.option(
        "--oauth-mcp-base-url",
        "oauth_mcp_base_url",
        envvar="CB_MCP_OAUTH_MCP_BASE_URL",
        default=None,
        help="Public base URL of this MCP server (e.g. https://api.yourcompany.com). "
        "When set, the server publishes RFC 9728 Protected Resource Metadata at "
        "<base_url>/.well-known/oauth-protected-resource/mcp so PRM-aware clients "
        "can discover the authorization server and perform DCR directly against it. "
        "Optional — omit to run as a JWT-validating resource server only.",
    ),
    click.option(
        "--oauth-scope-read-label",
        "oauth_scope_read",
        envvar="CB_MCP_OAUTH_SCOPE_READ_LABEL",
        default=SCOPE_READ,
        callback=validate_scope_label,
        help="Override the OAuth scope label the server treats as 'read' access. "
        "Use this when your IdP cannot emit the canonical scope form. "
        "The configured value is advertised in PRM and accepted in the token "
        "'scope'/'scp' claims. A blank/invalid value warns and falls back to the "
        "default.",
    ),
    click.option(
        "--oauth-scope-write-label",
        "oauth_scope_write",
        envvar="CB_MCP_OAUTH_SCOPE_WRITE_LABEL",
        default=SCOPE_WRITE,
        callback=validate_scope_label,
        help="Override the OAuth scope label for 'write' access. "
        "Same semantics as --oauth-scope-read-label.",
    ),
)
"""OAuth resource-server configuration. Shared by every server; scope label defaults are per-server."""
