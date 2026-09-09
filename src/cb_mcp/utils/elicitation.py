"""
Elicitation utilities for MCP tool confirmation.

Provides a wrapper to require user confirmation before executing
high-risk tools, using MCP's elicitation protocol.
"""

import functools
import inspect
import logging
from collections.abc import Callable

from mcp import types
from pydantic import BaseModel, Field

from .constants import LOGGER_ROOT

logger = logging.getLogger(f"{LOGGER_ROOT}.utils.elicitation")


class ConfirmationResult(BaseModel):
    """Schema for confirmation elicitation requests."""

    confirm: bool = Field(
        default=True,
        title="Confirm Execution",
        description="Set to true to confirm execution of this tool",
    )


def _build_confirmation_message(tool_name: str, kwargs: dict) -> str:
    """Build a human-readable confirmation message for a tool invocation."""
    # Extract key identifiers from common parameters for a descriptive message
    parts = [f"Do you want to execute '{tool_name}'?"]

    identifiers = []
    for key in ("document_id", "bucket_name", "scope_name", "collection_name"):
        if key in kwargs:
            identifiers.append(f"{key}={kwargs[key]}")

    if identifiers:
        parts.append(f"Parameters: {', '.join(identifiers)}")

    return " ".join(parts)


def _client_supports_elicitation(ctx) -> bool:
    """Return True when client explicitly advertises elicitation capability."""
    session = getattr(ctx.request_context, "session", None)
    if session is None or not hasattr(session, "check_client_capability"):
        return False

    return session.check_client_capability(
        types.ClientCapabilities(elicitation=types.ElicitationCapability())
    )


def wrap_with_confirmation(fn: Callable) -> Callable:
    """Wrap a tool function with elicitation-based confirmation.

    This wrapper prompts for user confirmation via MCP elicitation
    before executing the wrapped tool.

    If the client does not support elicitation, the tool executes without
    confirmation to maintain backward compatibility.
    """

    fn_signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        """Apply confirmation gate, then delegate to the wrapped tool.

        High-level flow:
        1) Reconstruct a name->value argument map from positional/keyword inputs.
        2) Read `ctx` from that normalized map and run elicitation when supported.
        3) Preserve the wrapped function's original calling style (async or sync).
        """
        # `bind_partial` maps runtime `*args/**kwargs` to the wrapped function's
        # declared parameter names without requiring every optional parameter.
        # This gives us a reliable `{"ctx": ..., "document_id": ...}` view.
        bound_args = fn_signature.bind_partial(*args, **kwargs)
        call_arguments = dict(bound_args.arguments)

        # Tools in this codebase consistently use `ctx` for FastMCP Context.
        ctx = call_arguments.get("ctx")

        if ctx:
            tool_name = fn.__name__
            if not _client_supports_elicitation(ctx):
                logger.debug(
                    f"Client does not advertise elicitation support for '{tool_name}'; "
                    "proceeding without confirmation"
                )
            else:
                message = _build_confirmation_message(tool_name, call_arguments)
                result = await ctx.elicit(
                    message=message,
                    schema=ConfirmationResult,
                )

                if result.action != "accept" or (
                    hasattr(result, "data") and result.data and not result.data.confirm
                ):
                    msg = f"Execution of '{tool_name}' was not confirmed by the user."
                    logger.warning(msg)
                    raise PermissionError(msg)

                logger.info(f"User confirmed execution of '{tool_name}'")

        # Keep wrapper behavior transparent: await async tools, call sync tools directly.
        if inspect.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        return fn(*args, **kwargs)

    return wrapper
