"""Deployment posture: whether a human is actually there when a tool runs.

Two shapes of deployment run this server, and they differ in one fact that
nothing in the configuration currently records: whether a person is sitting at
the MCP client at the moment a tool is called.

``workstation``
    A developer or field engineer runs the server as a child process of their
    own MCP client, over stdio. One human, one session. A confirmation prompt
    is a real second look before a write lands, and the operating-system user
    is the audit trail.

``enterprise``
    The server listens on a network transport and answers a service principal.
    Nobody is watching. A confirmation prompt is answered by the same model
    that asked for the tool, so it is not a control at all; scopes, the
    disabled-tool list and the audit trail are.

The distinction matters because a control that works in one posture is
decorative in the other, and the failure is silent. An enterprise deployment
leaning on ``--confirmation-required-tools`` believes it has a gate in front of
its write tools. It does not.

What this module does is deliberately small. It changes no tool's behaviour and
registers nothing. It lets an operator *declare* which deployment this is, and
then refuses to start when the declaration and the rest of the configuration
disagree: at startup, on the operator's terminal, rather than at 3am in a log
nobody is reading.

The declaration is optional. Unset is exactly today's behaviour plus one INFO
line, so no existing deployment, container or config changes.

One rule is worth stating twice, because it is the thing that is easy to get
wrong: presence is a property of the *transport*, not of the label. A
deployment that calls itself a workstation while listening on HTTP is not a
workstation, whatever it says. ``human_is_present`` therefore reads the
transport and nothing else, and the checks below are what keep the label
honest.
"""

import logging

from .constants import (
    ALLOWED_SESSION_POSTURES,
    LOGGER_NAMESPACE,
    NETWORK_TRANSPORTS,
    SESSION_POSTURE_ENTERPRISE,
    SESSION_POSTURE_WORKSTATION,
)

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.utils.posture")

__all__ = [
    "PostureConfigError",
    "human_is_present",
    "validate_posture",
]


class PostureConfigError(ValueError):
    """A declared posture contradicts the rest of the configuration.

    Raised rather than logged: an operator who declared a posture asked for
    the declaration to mean something, and starting anyway would hand them a
    server whose controls do not do what the flag says they do. The host
    translates this into a usage error (see ``CliParams.resolve_posture``),
    so what reaches the terminal is a message and not a traceback.
    """


def human_is_present(transport: str) -> bool:
    """Whether a person is at the MCP client when a tool is called.

    Reads the transport, and nothing else. Not the declared posture, and not
    whether confirmation is configured: stdio means this process was spawned
    by one interactive client and lives and dies with it, while a network
    transport means it answers whoever connects, which in practice is a
    service principal driven by an agent.

    This is the predicate the posture checks are written against, and the
    reason a label alone cannot make a deployment attended.
    """
    return transport not in NETWORK_TRANSPORTS


def _check_workstation(transport: str) -> None:
    """A workstation must be attended, which means a non-network transport."""
    if not human_is_present(transport):
        raise PostureConfigError(
            f"--session-posture={SESSION_POSTURE_WORKSTATION} declares that a human "
            f"is at the MCP client, but --transport={transport} serves whoever "
            "connects. Use --transport=stdio, or declare "
            f"--session-posture={SESSION_POSTURE_ENTERPRISE}."
        )


def _check_enterprise(
    transport: str,
    *,
    oauth_enabled: bool,
    confirmation_required_tools: frozenset[str],
) -> None:
    """An enterprise deployment is unattended, so its controls must not need a human."""
    if human_is_present(transport):
        raise PostureConfigError(
            f"--session-posture={SESSION_POSTURE_ENTERPRISE} declares an unattended "
            f"service, but --transport={transport} runs this server as a child "
            "process of a single interactive client. Use a network transport, "
            f"or declare --session-posture={SESSION_POSTURE_WORKSTATION}."
        )
    if not oauth_enabled:
        raise PostureConfigError(
            f"--session-posture={SESSION_POSTURE_ENTERPRISE} declares an unattended "
            "service, but OAuth is not active, so no scope is checked on any "
            "call and every connecting client gets every registered tool. "
            "Configure the OAuth flags (--oauth-jwks-uri, --oauth-issuer, "
            "--oauth-audience), or declare "
            f"--session-posture={SESSION_POSTURE_WORKSTATION}."
        )
    if confirmation_required_tools:
        named = ", ".join(sorted(confirmation_required_tools))
        raise PostureConfigError(
            f"--session-posture={SESSION_POSTURE_ENTERPRISE} declares an unattended "
            f"service, but --confirmation-required-tools names {named}. An "
            "elicitation prompt is answered by the same model that asked for "
            "the tool when no human is present, so it gates nothing. Withhold "
            "those tools with --disabled-tools or with the write scope, or "
            f"declare --session-posture={SESSION_POSTURE_WORKSTATION}."
        )


def validate_posture(
    posture: str | None,
    *,
    transport: str,
    oauth_enabled: bool,
    confirmation_required_tools: frozenset[str] | set[str] = frozenset(),
) -> str | None:
    """Refuse to start when the declared posture contradicts the configuration.

    Returns ``posture`` unchanged on success, so a caller can report what was
    declared without reaching for the raw flag a second time.

    ``None`` means nothing was declared. That is not an error and never will
    be: the flag was added after these deployments existed, and making it
    required would break every container and client config that predates it.
    An undeclared deployment is recorded at INFO and left alone.

    ``confirmation_required_tools`` is the *resolved* name set, after gating,
    not the string the operator typed. A name that matched no tool was already
    dropped by then, so this cannot refuse a start over a typo that has no
    effect.
    """
    if posture is None:
        logger.info(
            "No deployment posture declared; configuration unchecked "
            "(transport=%s, human_is_present=%s). Declare one with "
            "--session-posture (%s) to have it checked at startup.",
            transport,
            human_is_present(transport),
            ", ".join(ALLOWED_SESSION_POSTURES),
        )
        return None

    if posture == SESSION_POSTURE_WORKSTATION:
        _check_workstation(transport)
    elif posture == SESSION_POSTURE_ENTERPRISE:
        _check_enterprise(
            transport,
            oauth_enabled=oauth_enabled,
            confirmation_required_tools=frozenset(confirmation_required_tools),
        )
    else:
        # Unreachable through the CLI, which restricts the flag to
        # ALLOWED_SESSION_POSTURES. Explicit anyway: a third posture added to that
        # tuple and not handled here would otherwise be checked as whichever
        # branch happened to be last, which is the silent half of the failure
        # this module exists to make loud.
        raise PostureConfigError(
            f"Unknown deployment posture {posture!r}; expected one of "
            f"{', '.join(ALLOWED_SESSION_POSTURES)}."
        )

    logger.info(
        "Deployment posture %s: transport=%s, human_is_present=%s, "
        "oauth_enabled=%s, confirmation_required_tools=%d",
        posture,
        transport,
        human_is_present(transport),
        oauth_enabled,
        len(confirmation_required_tools),
    )
    return posture
