"""Helpers that turn a CAS argument into Couchbase SDK options for KV calls."""

from typing import Any


def parse_cas(cas: str) -> int:
    """Parse a CAS value supplied as a decimal string into an int.

    CAS crosses the tool boundary as a *string* because it is an unsigned
    64-bit value: above 2**53 a JSON number round-trip silently alters it, and
    a concurrency guard built on an altered CAS is worse than none at all - it
    would fail against the very revision it was meant to match.

    Validation is strict rather than relying on int(), which would accept
    "1_0", " 10 " and "+10". Tool parameters are LLM-generated, so a value
    that looks plausible but means something else is exactly the failure mode
    worth closing here.
    """
    if not isinstance(cas, str) or not cas.isdigit():
        raise ValueError(
            "cas must be a decimal string of an unsigned 64-bit integer "
            f"(digits only); got {cas!r}"
        )
    parsed = int(cas)
    if parsed == 0:
        # The SDK treats a CAS of 0 as the sentinel for "no CAS check", so a
        # zero here would turn a guarded write into an unconditional one while
        # still looking guarded to the caller. Zero is never a CAS returned for
        # an existing document, so no legitimate caller reaches this.
        raise ValueError(
            "cas must be a CAS value read from an existing document; 0 means "
            '"no CAS check" to the SDK and would make this write unconditional'
        )
    if parsed >= 2**64:
        raise ValueError(f"cas must fit in an unsigned 64-bit integer; got {parsed}")
    return parsed


def cas_options(option_cls: Any, cas: str | None) -> Any:
    """Build an SDK options object carrying a CAS, or None if none is given.

    Returning None when no CAS is requested keeps the call path identical to
    the one used before this parameter existed, so unguarded writes behave
    exactly as they did.
    """
    if cas is None:
        return None
    return option_cls(cas=parse_cas(cas))
