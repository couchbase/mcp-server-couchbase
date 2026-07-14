"""Click validators for CLI parameters.

These thin wrappers exist so multiple ``@click.command`` entrypoints can share identical
validation and fallback behaviour for common flags without re-implementing
the glue. The framework-agnostic parsing helpers live in
:mod:`cb_mcp.utils.logging`; this module keeps the ``click`` import isolated
to the layer that actually needs it.

Usage::

    @click.option("--log-level", callback=validate_log_level, ...)
    @click.option("--log-sinks", callback=validate_log_sinks, ...)
"""

from logging import getLogger

import click

from .constants import MCP_SERVER_NAME
from .logging import parse_log_level, parse_log_sinks

logger = getLogger(f"{MCP_SERVER_NAME}.utils.cli")


def validate_log_level(
    ctx: click.Context, param: click.Parameter, value: str
) -> tuple[str, str | None]:
    """Click callback for ``--log-level``.

    Delegates to :func:`parse_log_level`, which falls back to the default
    level on invalid input and returns the original token so
    ``configure_logging`` can surface an error record once handlers are wired.
    """
    return parse_log_level(value)


def validate_log_sinks(
    ctx: click.Context, param: click.Parameter, value: str
) -> tuple[set[str], list[str]]:
    """Click callback for ``--log-sinks``.

    Delegates to :func:`parse_log_sinks`, which keeps valid tokens, collects
    invalid ones for later reporting, and falls back to the default sink set
    when nothing valid survives.
    """
    return parse_log_sinks(value)


def validate_log_path(ctx: click.Context, param: click.Parameter, value: str) -> str:
    """Click callback for ``--log-file`` (the base path for per-level files).

    Trims whitespace and rejects empty strings via :exc:`click.BadParameter`.
    Unlike level/sink validation, an empty path is structurally invalid (we
    have no way to interpret it) and warrants a loud rejection rather than a
    silent fallback. The Click default still applies when the flag is omitted
    entirely.
    """
    trimmed = value.strip() if value else ""
    if not trimmed:
        raise click.BadParameter(
            "path cannot be empty; either omit the flag to use the default, "
            "or provide a non-empty path."
        )
    return trimmed


def validate_scope_label(
    ctx: click.Context, param: click.Parameter, value: object
) -> str:
    """Click callback for the OAuth scope-label options
    (``--oauth-scope-read-label`` / ``--oauth-scope-write-label``).

    Returns the trimmed label when the operator supplies a usable, non-empty
    string. Otherwise it warns and falls back to the option's default — the
    canonical ``couchbase-mcp:read`` / ``couchbase-mcp:write`` scope. Click
    always delivers CLI/env input as ``str``, so in practice this guards
    blank/whitespace values; the ``isinstance`` check is defensive against a
    non-string default override or a programmatic caller.

    Unlike ``--log-file`` (which rejects empties loudly via ``BadParameter``),
    an unusable scope label is non-fatal — the server stays functional on the
    canonical default — so we warn and continue rather than abort startup.

    Note: callbacks run during Click argument parsing, before
    ``configure_logging`` wires the handlers, so this warning is emitted via
    Python logging's last-resort stderr handler. That is intentional — a
    misconfigured scope label should surface at startup regardless of the
    log-sink configuration.
    """
    default = param.default
    if isinstance(value, str) and value.strip():
        return value.strip()
    flag = param.opts[0] if param.opts else (param.name or "scope label")
    logger.warning(
        "Invalid value %r for %s; expected a non-empty string. "
        "Falling back to the default scope %r.",
        value,
        flag,
        default,
    )
    return default
