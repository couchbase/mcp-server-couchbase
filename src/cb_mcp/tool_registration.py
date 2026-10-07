"""
Tool registration orchestration shared across MCP implementations.
"""

import logging
from collections.abc import Callable, Mapping

from .core.spec import Deployment, ServerSpec
from .utils import wrap_with_telemetry
from .utils.config import parse_tool_names
from .utils.constants import LOGGER_NAMESPACE
from .utils.elicitation import wrap_with_confirmation
from .utils.scope_enforcement import (
    required_scopes_for_tool,
    wrap_with_scope_check,
)

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.tool_registration")


def unsupported_for_deployment(
    loaded_tool_names: set[str],
    requirements: Mapping[str, Deployment],
    deployment: Deployment | None,
) -> set[str]:
    """The loaded tools that cannot work on *deployment*.

    Empty when ``deployment`` is ``None``: a host that cannot tell what it is
    connected to withholds nothing, because the cost of guessing wrong is a
    tool the operator can never reach and no error explaining why. Tools whose
    requirement matches, and tools with no requirement at all, are kept.

    Intersected with ``loaded_tool_names`` so the result describes what was
    actually withheld from *this* registration — a write tool already absent
    under read-only mode is not reported a second time under a different
    reason.
    """
    if deployment is None:
        return set()
    return {
        name
        for name, required in requirements.items()
        if required is not deployment and name in loaded_tool_names
    }


def prepare_tools_for_registration(
    spec: ServerSpec,
    read_only_mode: bool,
    disabled_tools: str | None,
    confirmation_required_tools: str | None,
    enforce_scopes: bool = False,
    deployment: Deployment | None = None,
) -> tuple[list[Callable], set[str], set[str]]:
    """Prepare final tool list and confirmation configuration for registration.

    Loads the shared cb_mcp tools, parses the disabled and confirmation lists,
    filters disabled tools out, and wraps tools with elicitation and (when
    OAuth is active) per-tool scope enforcement.

    Wrap order is ``scope_check ⟶ confirmation ⟶ telemetry ⟶ tool``: the scope
    check runs first so unauthorized callers never trigger an elicitation
    prompt. Scope checks are no-ops at runtime when no access token is present
    (stdio / unauthenticated), so ``enforce_scopes`` only affects whether
    the wrapper is installed — not whether it does work per call. Telemetry
    is innermost so its recorded duration/success reflects only the tool's
    own execution, excluding confirmation/scope-check overhead. A call
    rejected by the scope check or declined at confirmation never reaches
    the tool, so it never emits a tool-call event.

    ``spec`` says *which* server is being registered. It is taken whole rather
    than as separate tool-set / scope / hint arguments so those cannot drift
    apart: pairing one server's tools with another's scope labels would gate
    them on a scope no token will ever carry, and nothing would report it. A
    caller wanting a subset of a server's tools should narrow the spec —
    ``dataclasses.replace(SPEC, tools=...)`` — rather than pass pieces.

    Taking the spec also means this module no longer imports any server, so it
    stays free of SDK imports at module load.

    ``deployment`` is the third gate, after read-only mode and the operator's
    opt-out list, and the only one the operator does not choose: a tool the
    spec marks as working on one deployment only is withheld when the host
    resolved a different one. It is reported through ``disabled_tools`` rather
    than a set of its own — the effect is identical (not registered, so never
    offered and never called), and every consumer of that set already says the
    right thing about it. ``None`` — a host that cannot tell — withholds
    nothing, which is why a tool with a real deployment constraint keeps its
    own runtime check as well.
    """
    # When read_only_mode is True, write tools (KV, collection management, and
    # index management) are not loaded.
    tools = spec.tools.tools_for(read_only_mode=read_only_mode)

    loaded_tool_names = {tool.__name__ for tool in tools}

    # Parsed against the full loaded set, before deployment filtering, so an
    # operator who disables a tool this deployment would have withheld anyway
    # gets no spurious "unknown tool" warning for naming a real one.
    disabled_tool_names = parse_tool_names(disabled_tools, loaded_tool_names)

    unsupported_tool_names = unsupported_for_deployment(
        loaded_tool_names, spec.deployment_requirements, deployment
    )
    if unsupported_tool_names:
        # Logged separately from the operator's own list: these were not asked
        # for, and an operator reading the disabled set in the status tool
        # needs this line to explain where the extra names came from.
        logger.info(
            "Deployment %r does not support %d tool(s); withholding them: %s",
            deployment.value if deployment else None,
            len(unsupported_tool_names),
            sorted(unsupported_tool_names),
        )
    # Merged rather than reported apart: both are tools deliberately not
    # registered, and the status tool, the audit SERVER_CONFIGURATION record
    # and the startup log already carry exactly that set.
    disabled_tool_names |= unsupported_tool_names

    if disabled_tool_names:
        logger.info(
            f"Disabled {len(disabled_tool_names)} tool(s): {sorted(disabled_tool_names)}"
        )

    configured_confirmation_tool_names = parse_tool_names(
        confirmation_required_tools, loaded_tool_names
    )

    if configured_confirmation_tool_names:
        logger.info(
            f"Confirmation required for {len(configured_confirmation_tool_names)} tool(s): "
            f"{sorted(configured_confirmation_tool_names)}"
        )

    enabled_tools = [tool for tool in tools if tool.__name__ not in disabled_tool_names]

    # Apply confirmation only to tools that are actually active.
    active_tool_names = {tool.__name__ for tool in enabled_tools}
    active_confirmation_tool_names = (
        configured_confirmation_tool_names & active_tool_names
    )

    skipped_confirmation_tool_names = (
        configured_confirmation_tool_names - active_tool_names
    )
    if skipped_confirmation_tool_names:
        logger.info(
            "Skipped confirmation for unavailable tool(s): "
            f"{sorted(skipped_confirmation_tool_names)}"
        )

    write_tool_names = spec.tools.write_tool_names

    final_tools: list[Callable] = []
    for tool in enabled_tools:
        wrapped = wrap_with_telemetry(tool, server_id=spec.id)
        if tool.__name__ in active_confirmation_tool_names:
            wrapped = wrap_with_confirmation(wrapped)
        if enforce_scopes:
            required_scopes = required_scopes_for_tool(
                tool.__name__, write_tool_names=write_tool_names, scopes=spec.scopes
            )
            wrapped = wrap_with_scope_check(
                wrapped,
                required_scopes,
                hint=spec.scope_hints.get(tool.__name__),
            )
        final_tools.append(wrapped)

    if enforce_scopes:
        logger.info(
            "Per-tool OAuth scope enforcement enabled for %d tool(s).",
            len(enabled_tools),
        )

    return final_tools, configured_confirmation_tool_names, disabled_tool_names
