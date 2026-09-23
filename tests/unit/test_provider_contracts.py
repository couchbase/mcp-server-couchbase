"""Every shipped provider implements the protocol its server expects.

These protocols are structural and not ``runtime_checkable``, deliberately:
such a check verifies member *names* only, never signatures or types, so
unrelated providers sharing those names all pass it — which made
``isinstance`` against the old ``ClusterProvider`` actively misleading (its
own docstring said never to use it). Dropping it, though, left nothing at
all checking that a provider and its protocol agree until a tool call blew
up at runtime.

This module is that check, done honestly: it reads the members off each
protocol and asserts the concrete provider actually has them. It is not a
type check — only a static checker can compare signatures — but it catches
the failure that actually happens in practice, which is a member that is
missing, renamed, or (for ``handle_registry``) never assigned in ``__init__``.

Importing both providers here pulls in both SDKs. That is fine in a test
process and deliberate in exactly the same way ``tests/_all_specs.py`` is —
see ``tests/unit/test_sdk_isolation.py`` for the rule this does not violate.
"""

from __future__ import annotations

from typing import Any, Generic, Protocol

import pytest

from cb_mcp.core.contracts import ClusterProvider, ProviderLifecycle
from cb_mcp.utils.operational_insights.contracts import OperationalInsightsProvider
from providers.operational_insights import OperationalInsightsClusterProvider
from providers.static import StaticClusterProvider

#: Settings are never read at construction time — every provider connects
#: lazily on the first ``get_cluster`` — so an empty mapping is enough to
#: build one and inspect its instance attributes.
_NO_SETTINGS: dict[str, Any] = {}


def _protocol_members(protocol: type) -> set[str]:
    """Public members a protocol declares, including inherited ones.

    Walks the MRO rather than using ``__protocol_attrs__``, which only
    exists on Python 3.12+; this project supports 3.10. Annotations are
    collected alongside ``vars`` so attribute members (``handle_registry``)
    count, not just methods.
    """
    members: set[str] = set()
    for klass in protocol.__mro__:
        if klass in (object, Protocol, Generic):
            continue
        members |= {name for name in vars(klass) if not name.startswith("_")}
        members |= {
            name
            for name in getattr(klass, "__annotations__", {})
            if not name.startswith("_")
        }
    return members


def test_lifecycle_protocol_declares_exactly_what_shared_code_calls():
    """A guard on the split itself, not on any provider.

    ``ProviderLifecycle`` exists to be the *service-agnostic* half. If
    ``get_cluster`` ever reappears here, the shared layer is back to naming
    an SDK type and ``cb_mcp.core`` stops being importable without it.
    """
    assert _protocol_members(ProviderLifecycle) == {
        "close",
        "get_configuration",
        "is_connected",
    }


@pytest.mark.parametrize(
    ("provider_cls", "protocol"),
    [
        pytest.param(StaticClusterProvider, ClusterProvider, id="operational"),
        pytest.param(
            OperationalInsightsClusterProvider,
            OperationalInsightsProvider,
            id="operational-insights",
        ),
    ],
)
def test_provider_implements_its_protocol(provider_cls, protocol):
    """Each provider has every member its own protocol declares."""
    provider = provider_cls(settings=_NO_SETTINGS)
    missing = {
        member
        for member in _protocol_members(protocol)
        if not hasattr(provider, member)
    }
    assert not missing, (
        f"{provider_cls.__name__} is missing {sorted(missing)} required by "
        f"{protocol.__name__}. Attribute members must be assigned in "
        "__init__, not just annotated."
    )


@pytest.mark.parametrize(
    "provider_cls",
    [
        pytest.param(StaticClusterProvider, id="operational"),
        pytest.param(OperationalInsightsClusterProvider, id="operational-insights"),
    ],
)
def test_provider_satisfies_the_shared_lifecycle(provider_cls):
    """Whatever else it offers, the shared machinery's three calls must land.

    ``build_app``'s lifespan calls ``close()``; the status tool calls
    ``get_configuration()`` and ``is_connected()``. A provider that satisfies
    only its own service's protocol and not this one would fail at teardown,
    which is the least observable place to fail.
    """
    provider = provider_cls(settings=_NO_SETTINGS)
    for member in _protocol_members(ProviderLifecycle):
        assert callable(getattr(provider, member, None)), (
            f"{provider_cls.__name__}.{member} is missing or not callable; "
            "the shared lifespan and status tool both rely on it"
        )


def test_insights_provider_registry_is_per_instance():
    """``handle_registry`` is instance state, not a class attribute.

    Two servers sharing one registry would let a token minted by one be
    redeemed against the other's cluster. The protocol can only say the
    member exists; this says it is not shared.
    """
    first = OperationalInsightsClusterProvider(settings=_NO_SETTINGS)
    second = OperationalInsightsClusterProvider(settings=_NO_SETTINGS)
    assert first.handle_registry is not second.handle_registry
