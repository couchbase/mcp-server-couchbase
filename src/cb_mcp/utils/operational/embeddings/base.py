"""The embedding-provider interface: one ABC every provider implements.

An ABC rather than a ``Protocol`` — deliberately different from
``core/contracts.py``'s ``ProviderLifecycle``/``ClusterProvider``. Those are
``Protocol``s because a host *outside this repo* (a managed Capella runtime)
may implement them structurally. Every embedding provider lives inside this
same package, so there is no cross-repo contract to keep structural, and an
ABC catches a forgotten ``@abstractmethod`` override at class-definition time
rather than only at the call site.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from .registry import ProviderConfigDoc


@dataclass(frozen=True)
class EmbeddingRequest:
    """What a tool asks an embedding provider to do."""

    text: str
    model: str
    #: These tools only ever embed a search query, never a document to index.
    input_type: str = "query"


@dataclass(frozen=True)
class EmbeddingResult:
    """What a provider hands back — bounded, never the raw HTTP response."""

    vector: list[float]
    #: The model the provider actually used (may differ from the request, e.g. a
    #: Bedrock ARN resolved from a shorthand model ID).
    model: str
    dimensions: int


class EmbeddingConfigError(ValueError):
    """Config is missing or invalid for the selected provider.

    A ``ValueError`` subclass, not a bare ``Exception``, so the same
    ``except Exception`` already used in every tool body catches it and turns
    it into ``tool_error()`` without a provider-specific except clause, while
    still being distinguishable by type in tests.
    """


class EmbeddingProvider(ABC):
    """One embedding backend. An instance is fully configured — no settings
    mapping is threaded through after construction.
    """

    #: The EMBEDDING_PROVIDER value that selects this class. Matched
    #: case-insensitively by the registry.
    provider_id: ClassVar[str]

    @classmethod
    @abstractmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> EmbeddingProvider:
        """Build a configured instance from the flat ``settings`` mapping.

        Reads only ``embedding_*`` keys — never ``os.environ`` — per the
        host-agnostic rule (``cb_mcp`` never reads CLI/env configuration
        directly; ``settings`` is what the host already resolved it into).
        Must raise :class:`EmbeddingConfigError`, naming every missing/invalid
        field at once via :func:`require_fields`, not just the first.
        """

    @abstractmethod
    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """Call the embedding backend. Tools in this server are sync
        functions (no ``async def`` anywhere in ``tools/operational``), so
        this is sync too.

        Must raise on any failure (network error, non-2xx response,
        malformed body) rather than returning a partial/garbage vector —
        callers catch it themselves, same convention as every other
        tool-body ``try``/``except`` in this codebase.
        """

    @classmethod
    @abstractmethod
    def describe_config(cls) -> ProviderConfigDoc:
        """Structured documentation for this provider's config fields.

        Returns ``PROVIDER_CONFIG_DOCS[cls.provider_id]`` — the single source
        of truth :func:`require_fields` reads from to validate settings.
        """


def require_fields(
    settings: Mapping[str, Any], doc: ProviderConfigDoc
) -> dict[str, Any]:
    """Pull ``doc``'s fields out of ``settings``, raising if any required one is missing.

    Shared by every provider's ``from_settings`` so the missing-field check
    (and its error message) is written once. Names every missing field in a
    single error, not just the first, so a caller fixing config doesn't
    discover them one at a time.
    """
    values = {
        field.settings_key: settings.get(field.settings_key) for field in doc.fields
    }
    missing = [
        field.name
        for field in doc.fields
        if field.required and not values.get(field.settings_key)
    ]
    if missing:
        raise EmbeddingConfigError(
            f"Missing required settings for provider {doc.provider_id!r}: "
            f"{', '.join(missing)}"
        )
    return values
