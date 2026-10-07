"""OpenAI embedding provider (BYOM).

``EMBEDDING_ENDPOINT`` also covers "OpenAI-compatible local server" (Ollama,
vLLM, LM Studio) — point it at the local server's base URL with
``EMBEDDING_PROVIDER=openai``; no separate provider is needed for that case.
"""

from __future__ import annotations

from ..registry import PROVIDER_CONFIG_DOCS, ProviderConfigDoc
from ._openai_compatible import _OpenAICompatibleProvider


class OpenAIEmbeddingProvider(_OpenAICompatibleProvider):
    provider_id = "openai"
    _default_base_url = "https://api.openai.com/v1"

    @classmethod
    def describe_config(cls) -> ProviderConfigDoc:
        return PROVIDER_CONFIG_DOCS[cls.provider_id]
