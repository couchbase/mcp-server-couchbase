"""Helpers that turn tool arguments into Couchbase SDK options for KV calls."""

from datetime import timedelta
from typing import Any

from couchbase.durability import ServerDurability
from couchbase.exceptions import (
    DurabilityImpossibleException,
    DurabilitySyncWriteAmbiguousException,
)

from ...utils.responses import tool_error
from .constants import DURABILITY_LEVELS


class KvOptionError(ValueError):
    """An invalid KV write option, carrying the PRD error code for the caller."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def kv_error(error: Exception) -> dict[str, Any]:
    """Build a tool_error envelope, adding a stable ``code`` when one applies.

    The envelope is extended, not replaced: ``error`` still carries the message
    and ``code`` lets an agent pick the right recovery (fix an argument, lower
    the durability level, re-read to see whether an ambiguous write committed)
    instead of retrying blindly.
    """
    code = getattr(error, "code", None)
    if not isinstance(code, str):
        if isinstance(error, DurabilityImpossibleException):
            code = "durability_impossible"
        elif isinstance(error, DurabilitySyncWriteAmbiguousException):
            code = "sync_write_ambiguous"
        else:
            code = None
    return tool_error(error, code=code) if code else tool_error(error)


def result_cas(result: Any) -> str | None:
    """Return a mutation or read result's CAS as a decimal string, or None.

    CAS is an unsigned 64-bit value, so it is returned as a string: above 2**53
    a JSON number round trip alters it. A CAS of 0 means the server supplied
    none, and is reported as None so callers omit the key.
    """
    cas = getattr(result, "cas", 0)
    return str(cas) if cas else None


def with_cas(response: dict[str, Any], result: Any) -> dict[str, Any]:
    """Add ``cas`` to a response when the result carries one."""
    cas = result_cas(result)
    if cas is not None:
        response["cas"] = cas
    return response


def sdk_kv_options(
    option_cls: Any,
    durability: str | None = None,
    expiry_seconds: int | None = None,
    preserve_expiry: bool = False,
) -> Any:
    """Build an SDK options object for a KV write, or None if nothing is set.

    Returning None when no option is requested keeps the call path identical
    to the one used before these parameters existed, so default behavior is
    unchanged.

    preserve_expiry keeps a document's existing TTL through a write that does
    not set one. It cannot be combined with expiry_seconds.

    Raises KvOptionError (a ValueError) for an unknown durability level, a
    negative expiry, or expiry_seconds together with preserve_expiry, so the
    calling tool can report an actionable message rather than handing an
    invalid value to the SDK.
    """
    kwargs: dict[str, Any] = {}

    if preserve_expiry and expiry_seconds is not None:
        raise KvOptionError(
            "expiry_seconds and preserve_expiry cannot be used together: "
            "set a new expiry, or keep the existing one",
            "invalid_expiry",
        )

    if durability is not None:
        level = DURABILITY_LEVELS.get(durability)
        if level is None:
            raise KvOptionError(
                f"durability must be one of {sorted(DURABILITY_LEVELS)}; "
                f"got {durability!r}",
                "invalid_durability_level",
            )
        # The SDK reads the ``durability`` option key and derives the
        # wire-level durability_level from it. ``durability_level`` is not a
        # supported option key: the Options classes are unvalidated dict
        # subclasses, so passing it is accepted and then silently dropped,
        # which would turn the guarantee into a no-op.
        kwargs["durability"] = ServerDurability(level=level)

    if expiry_seconds is not None:
        if expiry_seconds < 0:
            raise KvOptionError(
                f"expiry_seconds must be zero or positive; got {expiry_seconds}",
                "invalid_expiry",
            )
        # Couchbase treats an expiry of 0 as "no expiry", which is also how
        # the SDK interprets a zero timedelta, so 0 clears an existing TTL.
        kwargs["expiry"] = timedelta(seconds=expiry_seconds)

    if preserve_expiry:
        kwargs["preserve_expiry"] = True

    if not kwargs:
        return None
    return option_cls(**kwargs)
