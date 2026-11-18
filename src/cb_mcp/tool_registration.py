"""
Tool registration orchestration shared across MCP implementations.
"""

import dataclasses
import logging
from collections.abc import Callable, Mapping

from .core.spec import Deployment, ServerSpec, ToolSet
from .tools.selection import make_select_tool_sets
from .utils.config import parse_tool_names
from .utils.constants import LOGGER_NAMESPACE
from .utils.elicitation import wrap_with_confirmation
from .utils.scope_enforcement import (
    required_scopes_for_tool,
    wrap_with_scope_check,
)

logger = logging.getLogger(f"{LOGGER_NAMESPACE}.tool_registration")


def _narrow_to_tool_sets(spec: ServerSpec, tool_sets: str | None) -> ServerSpec:
    """Return ``spec`` restricted to the named tool sets, or unchanged.

    ``--tool-sets`` is an allow list, which is the correct polarity once a
    catalogue is large: ``--disabled-tools`` is a deny list, and excluding most
    of a catalogue means naming most of it. The two compose — disabling still
    applies, as a subtraction after the sets are resolved.

    An unknown set name fails at startup, naming the sets that do exist. The
    alternative is loading nothing and leaving the operator to work out why,
    which is the failure mode this is meant to replace rather than reproduce.
    """
    if not tool_sets:
        return spec

    requested = [name.strip() for name in tool_sets.split(",") if name.strip()]
    if not requested:
        return spec

    unknown = [name for name in requested if name not in spec.tool_sets]
    if unknown:
        available = ", ".join(sorted(spec.tool_sets)) or "(none declared)"
        raise ValueError(
            f"Unknown tool set(s) {sorted(unknown)} for server '{spec.id}'. "
            f"Available: {available}."
        )

    selected: set[str] = set()
    for name in requested:
        selected |= set(spec.tool_sets[name])

    narrowed = ToolSet(
        read_only=tuple(fn for fn in spec.tools.read_only if fn.__name__ in selected),
        write=tuple(fn for fn in spec.tools.write if fn.__name__ in selected),
    )
    logger.info(
        "Tool sets %s selected for server '%s': %d of %d tool(s).",
        sorted(requested),
        spec.id,
        len(narrowed.all_tools),
        len(spec.tools.all_tools),
    )
    return dataclasses.replace(spec, tools=narrowed)


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
    tool_sets: str | None = None,
    enable_tool_set_selection: bool = False,
) -> tuple[list[Callable], set[str], set[str]]:
    """Prepare final tool list and confirmation configuration for registration.

    Loads the shared cb_mcp tools, parses the disabled and confirmation lists,
    filters disabled tools out, and wraps tools with elicitation and (when
    OAuth is active) per-tool scope enforcement.

    Wrap order is ``scope_check ⟶ confirmation ⟶ tool``: the scope check runs
    first so unauthorized callers never trigger an elicitation prompt. Scope
    checks are no-ops at runtime when no access token is present (stdio /
    unauthenticated), so ``enforce_scopes`` only affects whether the wrapper
    is installed — not whether it does work per call.

    ``spec`` says *which* server is being registered. It is taken whole rather
    than as separate tool-set / scope / hint arguments so those cannot drift
    apart: pairing one server's tools with another's scope labels would gate
    them on a scope no token will ever carry, and nothing would report it. A
    caller wanting a subset of a server's tools should narrow the spec —
    ``dataclasses.replace(SPEC, tools=...)`` — rather than pass pieces.

    ``tool_sets`` is an optional comma-separated list of names declared in
    ``spec.tool_sets``. When given, the spec is narrowed to the union of those
    sets before any other gating runs, so read-only mode, the disabled list,
    the deployment gate and scope enforcement all operate on the narrowed set
    rather than on a wider one that happens to be filtered later.

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
    # Narrow the spec before anything else reads it. This is the mechanism this
    # function's own docstring prescribes for a caller wanting a subset, made
    # selectable by name: resolve the requested sets, then
    # ``dataclasses.replace`` so every step below sees one coherent spec rather
    # than a spec plus a side list that could disagree with it.
    spec = _narrow_to_tool_sets(spec, tool_sets)

    # When read_only_mode is True, write tools (KV, collection management, and
    # index management) are not loaded.
    tools = spec.tools.tools_for(read_only_mode=read_only_mode)

    loaded_tool_names = {tool.__name__ for tool in tools}

    # Parsed against the full loaded set, before deployment filtering, so an
    # operator who disables a tool this deployment would have withheld anyway
    # gets no spurious "unknown tool" warning for naming a real one.
    disabled_tool_names = parse_tool_names(disabled_tools, loaded_tool_names)

    if deployment is not None:
        logger.info("Resolved deployment from connection string: %s", deployment.value)

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
        wrapped = tool
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

    if enable_tool_set_selection and spec.tool_sets:
        # Appended rather than declared in the spec's ToolSet, so a deployment
        # that leaves selection off registers exactly the tools it does today
        # and every existing count is untouched. It is wrapped like any other
        # tool: it is read-side, so a write-only token cannot reach it either.
        selector = make_select_tool_sets(spec)
        # Wrapped exactly as the loop above wraps the spec's own tools. v1.1
        # moved per-call telemetry into the audit middleware, so there is no
        # telemetry wrapper to apply here any more.
        #
        # It is deliberately absent from TOOL_CLASSIFICATION. That table is
        # asserted to cover the package's registered tool list exactly, and
        # this tool is not in that list: it is built per spec and appended
        # here, only when the operator opts in. So it takes classification's
        # fail-closed path and is recorded as a non-filterable write, which is
        # the correct reading of it in audit terms regardless -- it changes
        # which tools the session offers, so no deployment should be able to
        # filter the record of it away.
        wrapped = selector
        if enforce_scopes:
            wrapped = wrap_with_scope_check(
                wrapped,
                required_scopes_for_tool(
                    selector.__name__,
                    write_tool_names=write_tool_names,
                    scopes=spec.scopes,
                ),
                hint=spec.scope_hints.get(selector.__name__),
            )
        final_tools.append(wrapped)
        logger.info(
            "Agent-declared tool set selection enabled for server '%s'.", spec.id
        )

    return final_tools, configured_confirmation_tool_names, disabled_tool_names
