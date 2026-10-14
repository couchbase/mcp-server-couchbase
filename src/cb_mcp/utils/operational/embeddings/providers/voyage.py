"""Voyage AI embedding provider (BYOM).

Voyage's ``/v1/embeddings`` is close enough to OpenAI's own schema (see
_openai_compatible.py's module docstring) to be served by the shared
_OpenAICompatibleProvider base; input_type is the one field outside OpenAI's
own schema, carried via extra_body.
"""

from __future__ import annotations

from ..registry import PROVIDER_CONFIG_DOCS, ProviderConfigDoc
from ._openai_compatible import _OpenAICompatibleProvider


class VoyageEmbeddingProvider(_OpenAICompatibleProvider):
    provider_id = "voyage"
    _default_base_url = "https://api.voyageai.com/v1"
    _include_input_type = True

    @classmethod
    def describe_config(cls) -> ProviderConfigDoc:
        return PROVIDER_CONFIG_DOCS[cls.provider_id]
