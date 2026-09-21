"""Typed views over this host's parsed Click parameters.

``mcp_server.py`` declares the flags; this module is the mechanical half of
turning them into configuration. Click hands a command body a flat
``dict[str, Any]`` of ~33 entries keyed by long strings. Read straight, that
mapping is unreadable at the point of use and unverifiable anywhere: a typo in
``params["log_warning_retention_backup_count"]`` is a KeyError at startup, and
nothing says which flags belong together.

So each option stack in ``cb_mcp.core.cli.options`` gets exactly one frozen
dataclass here, with a ``from_click`` classmethod that performs every lookup
for that stack in one place. The grouping mirrors the option stacks one-to-one
on purpose: if a flag is added to a stack, the class that has to learn about it
is the one named after that stack.

This module also owns the two other mechanical jobs: composing the six stacks
into one ``server_options`` decorator, and assembling the ``settings`` mapping.
``settings`` is a wire contract — the env-info diagnostic record, the
``get_server_configuration_status`` tool and every provider read it by key — so
the assembly is written as one literal whose keys can be diffed at a glance,
not accumulated across the file.

A deliberate, named exception to CONTRIBUTING.md's "Host-agnostic design"
rule: "don't read CLI/env configuration inside ``cb_mcp``; that belongs to the
host (``src/mcp_server.py`` and ``src/providers/``)." This module's whole job
is turning parsed CLI params into configuration, which is exactly that. It
lives here instead of in ``src/providers/`` anyway, by deliberate choice, so
that "helper code should be grouped with other helper code" wins over the
host/library boundary for this one file. Consequence to hold onto: nothing
under ``cb_mcp`` other than ``mcp_server.py`` (the host) may import this
module — it is not part of the reusable surface a managed host can rely on,
and it must never be re-exported from ``cb_mcp/utils/__init__.py``, or every
other ``cb_mcp`` consumer would inherit a dependency on Click.

Deliberately not here: anything a reader needs in order to understand *what
the servers are*. That story stays in ``mcp_server.py``.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

import click
from fastmcp.server.auth import AuthProvider

from ..auth import OAuthConfigError, resolve_oauth
from ..core.cli.options import (
    compose,
    credential_options,
    logging_options,
    oauth_options,
    read_only_option,
    tool_gating_options,
    transport_options,
)
from ..core.spec import ServerSpec
from ..servers.operational_insights.cli import oi_credential_options
from ..tool_registration import prepare_tools_for_registration
from .logging import (
    ParsedLogLevel,
    ParsedLogSinks,
    configure_logging,
    get_resolved_logging_config,
)

__all__ = [
    "CLUSTER_CREDENTIALS",
    "INSIGHTS_CREDENTIALS",
    "CliParams",
    "CredentialProfile",
    "GatedTools",
    "build_settings",
    "gate_tools",
    "resolved_logging_snapshot",
    "server_options",
]


class CredentialProfile(NamedTuple):
    """One server's credential flags: the Click stack, and the settings keys it fills.

    The two halves are bound together because they must agree and there is no
    type that can enforce it: ``options`` decides which ``params`` keys exist,
    ``settings_keys`` decides which of them reach ``settings``. Naming a key
    the stack does not declare is a KeyError at startup; *omitting* one is
    worse — silent, and it drops a field out of the support-bundle diagnostic.
    Declaring both in one constant makes them a single edit.
    """

    options: Callable
    settings_keys: tuple[str, ...]

    def settings_from(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """The credential slice of ``settings``, in declaration order."""
        return {key: params[key] for key in self.settings_keys}


CLUSTER_CREDENTIALS = CredentialProfile(
    options=credential_options,
    settings_keys=(
        "connection_string",
        "username",
        "password",
        "ca_cert_path",
        "client_cert_path",
        "client_key_path",
    ),
)

#: Three keys, not six: Operational Insights speaks HTTP(S) and has no mTLS
#: client material. The difference is not cosmetic — ``ServerSpec`` for this
#: server classifies no extra secret keys, so emitting ``client_cert_path``
#: here would be an unclassified settings key (test_settings_classification).
INSIGHTS_CREDENTIALS = CredentialProfile(
    options=oi_credential_options,
    settings_keys=("connection_string", "username", "password"),
)


@dataclass(frozen=True)
class TransportParams:
    """``--transport``/``--host``/``--port``: where the server listens, if it listens."""

    transport: str
    host: str
    port: int

    @classmethod
    def from_click(cls, params: Mapping[str, Any]) -> "TransportParams":
        return cls(
            transport=params["transport"],
            host=params["host"],
            port=params["port"],
        )


@dataclass(frozen=True)
class GatingParams:
    """Which tools get registered at all: read-only mode plus the opt-out lists.

    The two lists stay as the operator typed them (comma-separated names or a
    file path); ``prepare_tools_for_registration`` parses and validates them
    against the tools that actually loaded.
    """

    read_only_mode: bool
    disabled_tools: str | None
    confirmation_required_tools: str | None

    @classmethod
    def from_click(cls, params: Mapping[str, Any]) -> "GatingParams":
        return cls(
            read_only_mode=params["read_only_mode"],
            disabled_tools=params["disabled_tools"],
            confirmation_required_tools=params["confirmation_required_tools"],
        )


@dataclass(frozen=True)
class OAuthParams:
    """OAuth resource-server coordinates. Non-secret: JWTs are verified against a public JWKS.

    Field names follow the *settings* keys, not the Click param names, because
    two of them disagree: the flags are ``--oauth-scope-read-label`` /
    ``--oauth-scope-write-label`` while Click stores them as ``oauth_scope_read``
    / ``oauth_scope_write``. ``from_click`` below is the single place that
    translation happens.
    """

    jwks_uri: str | None
    issuer: str | None
    audience: str | None
    algorithm: str
    mcp_base_url: str | None
    scope_read_label: str
    scope_write_label: str

    @classmethod
    def from_click(cls, params: Mapping[str, Any]) -> "OAuthParams":
        return cls(
            jwks_uri=params["oauth_jwks_uri"],
            issuer=params["oauth_issuer"],
            audience=params["oauth_audience"],
            algorithm=params["oauth_algorithm"],
            mcp_base_url=params["oauth_mcp_base_url"],
            scope_read_label=params["oauth_scope_read"],
            scope_write_label=params["oauth_scope_write"],
        )

    def as_settings(self, *, enabled: bool) -> dict[str, Any]:
        """The OAuth slice of ``settings``.

        ``enabled`` is passed in rather than derived from these fields:
        ``resolve_oauth`` returns ``None`` for non-http transports even when
        every JWT setting is present, and the diagnostic record must report
        whether OAuth is *active*, not whether it was configured.
        """
        return {
            "oauth_enabled": enabled,
            "oauth_jwks_uri": self.jwks_uri,
            "oauth_issuer": self.issuer,
            "oauth_audience": self.audience,
            "oauth_algorithm": self.algorithm,
            "oauth_mcp_base_url": self.mcp_base_url,
            "oauth_scope_read_label": self.scope_read_label,
            "oauth_scope_write_label": self.scope_write_label,
        }


#: Levels that have their own rotating file and therefore their own optional
#: size/retention override. Ordered as they appear in ``--help``.
_OVERRIDABLE_LEVELS = ("ERROR", "WARNING", "INFO", "DEBUG")


def _per_level(params: Mapping[str, Any], *, suffix: str) -> dict[str, Any]:
    """Collect ``--log-<level>-<suffix>`` into ``{"ERROR": value, ...}``.

    Only levels the operator set explicitly appear; the rest are absent, which
    is how ``configure_logging`` is told to inherit the global for them. That
    is why ``None`` is filtered rather than passed through: ``None`` in the
    mapping would be a value, not an absence.
    """
    return {
        level: value
        for level in _OVERRIDABLE_LEVELS
        if (value := params[f"log_{level.lower()}_{suffix}"]) is not None
    }


@dataclass(frozen=True)
class LoggingParams:
    """The 14 logging flags, resolved.

    ``level`` and ``sinks`` are not strings: their Click callbacks return
    ``ParsedLogLevel``/``ParsedLogSinks``, each carrying the resolved value
    *plus* whatever input was rejected. The rejected tokens are carried all the
    way into ``configure_logging`` rather than warned about here, because there
    are no handlers to warn through until it runs.
    """

    level: ParsedLogLevel
    sinks: ParsedLogSinks
    log_file: str
    rotation_max_size_mb: float | None
    max_bytes: int | None
    backup_count: int
    rotation_size_overrides: Mapping[str, float]
    backup_count_overrides: Mapping[str, int]

    @classmethod
    def from_click(cls, params: Mapping[str, Any]) -> "LoggingParams":
        return cls(
            level=params["log_level"],
            sinks=params["log_sinks"],
            log_file=params["log_file"],
            rotation_max_size_mb=params["log_rotation_max_size_mb"],
            max_bytes=params["log_max_bytes"],
            backup_count=params["log_retention_backup_count"],
            rotation_size_overrides=_per_level(params, suffix="rotation_max_size_mb"),
            backup_count_overrides=_per_level(params, suffix="retention_backup_count"),
        )

    def apply(self, *, sdk_log_hook: Callable[[str, int], None] | None) -> None:
        """Install handlers. The first thing a server does, so everything after it is logged.

        ``sdk_log_hook`` comes from the server's spec: which SDK's logs join
        our hierarchy is the server's business, not the logging module's.
        """
        configure_logging(
            level=self.level.level,
            sinks=self.sinks.sinks,
            log_file=self.log_file,
            log_rotation_max_size_mb=self.rotation_max_size_mb,
            log_max_bytes=self.max_bytes,
            log_backup_count=self.backup_count,
            log_rotation_size_overrides=self.rotation_size_overrides,
            log_backup_count_overrides=self.backup_count_overrides,
            invalid_level=self.level.invalid_token,
            invalid_sinks=self.sinks.invalid_tokens,
            sdk_log_hook=sdk_log_hook,
        )


@dataclass(frozen=True)
class CliParams:
    """Everything one invocation configured, grouped by the flag stack it came from.

    Built once at the top of a run so nothing downstream touches the raw
    mapping again. ``credentials`` is already the settings slice rather than a
    typed group: credentials are the one thing that genuinely differs per
    server, and nothing but ``settings`` ever reads them.
    """

    credentials: Mapping[str, Any]
    logging: LoggingParams
    transport: TransportParams
    gating: GatingParams
    oauth: OAuthParams

    @classmethod
    def from_click(
        cls, params: Mapping[str, Any], *, credentials: CredentialProfile
    ) -> "CliParams":
        return cls(
            credentials=credentials.settings_from(params),
            logging=LoggingParams.from_click(params),
            transport=TransportParams.from_click(params),
            gating=GatingParams.from_click(params),
            oauth=OAuthParams.from_click(params),
        )

    def resolve_auth(self, spec: ServerSpec) -> AuthProvider | None:
        """The OAuth provider, or ``None`` when OAuth is off or not honored.

        Needs the transport as well as the OAuth flags, which is why it lives
        here rather than on ``OAuthParams``. Misconfiguration is translated
        into a Click error so the operator gets a usage message, not a
        traceback.
        """
        try:
            return resolve_oauth(
                transport=self.transport.transport,
                jwks_uri=self.oauth.jwks_uri,
                issuer=self.oauth.issuer,
                audience=self.oauth.audience,
                algorithm=self.oauth.algorithm,
                base_url=self.oauth.mcp_base_url,
                scope_read=self.oauth.scope_read_label,
                scope_write=self.oauth.scope_write_label,
                resource_name=spec.display_name,
            )
        except OAuthConfigError as e:
            raise click.UsageError(str(e)) from e


class GatedTools(NamedTuple):
    """What survived gating, and what the operator gated.

    Field order matches ``prepare_tools_for_registration``'s return tuple; the
    names are the point — the bare 3-tuple reads identically whichever way the
    last two are bound.
    """

    tools: list[Callable]
    confirmation_required: set[str]
    disabled: set[str]


def gate_tools(
    spec: ServerSpec, gating: GatingParams, *, enforce_scopes: bool
) -> GatedTools:
    """Apply read-only mode and the operator's opt-out lists to the spec's tools."""
    return GatedTools(
        *prepare_tools_for_registration(
            spec,
            read_only_mode=gating.read_only_mode,
            disabled_tools=gating.disabled_tools,
            confirmation_required_tools=gating.confirmation_required_tools,
            enforce_scopes=enforce_scopes,
        )
    )


def build_settings(
    cli: CliParams, *, gated: GatedTools, oauth_enabled: bool
) -> dict[str, Any]:
    """Assemble the ``settings`` mapping the whole runtime reads.

    This mapping is a contract, not a convenience bag: it is what
    ``AppContext`` carries, what ``log_environment_info`` redacts into the
    support bundle, what ``get_server_configuration_status`` reports, and what
    the provider factory receives. A key added here and not classified on the
    ``ServerSpec`` is dropped from the diagnostic *silently* — see
    ``tests/unit/test_settings_classification.py``.

    Note the gated-tool entries are the *resolved name sets*, not the raw
    strings the operator typed: the record should say what was actually
    disabled, not what was requested.
    """
    settings = dict(cli.credentials)
    settings.update(
        {
            "read_only_mode": cli.gating.read_only_mode,
            "transport": cli.transport.transport,
            "host": cli.transport.host,
            "port": cli.transport.port,
            **cli.oauth.as_settings(enabled=oauth_enabled),
            "disabled_tools": gated.disabled,
            "confirmation_required_tools": gated.confirmation_required,
        }
    )
    return settings


def resolved_logging_snapshot() -> dict[str, Any] | None:
    """What logging actually ended up doing, for the app to report.

    Reads the singleton ``configure_logging`` records, so it is only valid
    after ``LoggingParams.apply``. Returns ``None`` before that, which is also
    what ``build_app`` expects when a host has its own logging stack.
    """
    resolved = get_resolved_logging_config()
    return resolved.as_dict() if resolved else None


def server_options(
    *,
    credentials: CredentialProfile,
    default_port: int,
    default_log_file: str,
) -> Callable:
    """Every flag one server accepts, as a single decorator.

    Order is the whole point. It is what ``--help`` prints, it is deliberately
    interleaved rather than thematic (``--read-only-mode`` between credentials
    and transport; tool gating between ``--port`` and the logging flags), and
    before this factory it was retyped per server — so the second server's
    ``--help`` could drift from the first's with nobody noticing. Defining it
    once here means the only per-server inputs are the three that genuinely
    differ: whose credentials, which default port, which default log file.

    Apply this *above* ``@click.version_option``, as the six stacks were:
    ``--version`` is an eager option and its position in the list is asserted.
    """
    return compose(
        credentials.options,
        read_only_option,
        transport_options(default_port=default_port),
        tool_gating_options,
        logging_options(default_log_file=default_log_file),
        oauth_options,
    )
