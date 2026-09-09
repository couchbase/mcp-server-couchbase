"""A Click group that routes a bare invocation to a default subcommand.

The CLI exposes one subcommand per server. That has to happen without breaking
the invocation form every existing user, container and CI job already relies
on: ``couchbase-mcp-server`` with options and no subcommand, configured
entirely through environment variables.
"""

import click


class DefaultGroup(click.Group):
    """A ``click.Group`` that dispatches to ``default_cmd`` when no subcommand
    is given.

    The injection happens in :meth:`parse_args`, *before* delegating to
    ``click.Group.parse_args``. That placement is the whole trick, and the
    obvious alternative — overriding ``resolve_command`` — silently does not
    work, because two things reject the input before ``resolve_command`` is
    ever reached:

    * ``Group.parse_args`` raises ``NoArgsIsHelpError`` on empty argv, and
      ``Group.invoke`` fails with "Missing command." when nothing was
      dispatched. So a bare ``couchbase-mcp-server`` exits 2 — which would
      break the Docker ``ENTRYPOINT`` (no ``CMD``) and every env-var-only
      deployment.
    * The group's own parser knows only the group's parameters, so an unknown
      ``--transport`` raises ``NoSuchOption`` during the group's own parse.

    Prepending the subcommand name avoids both: the injected token is a
    non-option, and ``Group.allow_interspersed_args`` is ``False``, so the
    parser stops at it immediately and hands every remaining token to the
    subcommand untouched. No ``ignore_unknown_options`` is needed, which
    matters — that flag would suppress genuine typos.
    """

    def __init__(self, *args, default_cmd: str | None = None, **kwargs):
        # A group with a default is never "no args is help": empty argv is a
        # valid invocation that must reach the default subcommand.
        kwargs.setdefault("no_args_is_help", False)
        super().__init__(*args, **kwargs)
        self.default_cmd = default_cmd

    def _should_inject(self, ctx: click.Context, args: list[str]) -> bool:
        if not self.default_cmd:
            return False
        # Shell completion re-parses with resilient_parsing set. Injecting
        # there would hide every sibling subcommand from completion.
        if ctx.resilient_parsing:
            return False
        if args and args[0] in self.commands:
            return False
        # Let the group answer for itself rather than delegating these.
        return not (args and args[0] in (*ctx.help_option_names, "--version"))

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if self._should_inject(ctx, args):
            args = [self.default_cmd, *args]
        return super().parse_args(ctx, args)

    def format_options(self, ctx: click.Context, formatter) -> None:
        """Render group options, the command list, and the default's options.

        Without the last part, ``couchbase-mcp-server --help`` would document
        none of the ~35 options that a bare invocation actually accepts — the
        group itself declares almost none of them.
        """
        click.Command.format_options(self, ctx, formatter)
        self.format_commands(ctx, formatter)

        default = self.commands.get(self.default_cmd or "")
        if default is None:
            return
        own = {param.name for param in self.get_params(ctx)}
        rows = [
            record
            for param in default.get_params(ctx)
            if param.name not in own and (record := param.get_help_record(ctx))
        ]
        if rows:
            with formatter.section(
                f"Options for the default '{self.default_cmd}' server"
            ):
                formatter.write_dl(rows)
