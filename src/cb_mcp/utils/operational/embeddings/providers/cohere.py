"""Cohere embedding provider (BYOM). Cohere v2 requires ``input_type`` on every
request; hardcoded to ``"search_query"`` since these tools only ever embed a
search query, never a document to index."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from .....servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ..base import EmbeddingProvider, EmbeddingRequest, EmbeddingResult, require_fields
from ..registry import PROVIDER_CONFIG_DOCS, ProviderConfigDoc

logger = logging.getLogger(
    f"{OPERATIONAL_LOGGER_NAMESPACE}.utils.embeddings.providers.cohere"
)

_DEFAULT_BASE_URL = "https://api.cohere.com/v2"


class CohereEmbeddingProvider(EmbeddingProvider):
    provider_id = "cohere"

    def __init__(self, *, api_key: str, model: str, base_url: str) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url

    @classmethod
    def describe_config(cls) -> ProviderConfigDoc:
        return PROVIDER_CONFIG_DOCS[cls.provider_id]

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> CohereEmbeddingProvider:
        fields = require_fields(settings, cls.describe_config())
        base_url = (fields.get("embedding_endpoint") or _DEFAULT_BASE_URL).rstrip("/")
        return cls(
            api_key=fields["embedding_api_key"],
            model=fields["embedding_model"],
            base_url=base_url,
        )

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        model = request.model or self._model
        logger.debug(
            f"Requesting embedding from {self._base_url}/embed (model={model!r})"
        )
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{self._base_url}/embed",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": model,
                    "texts": [request.text],
                    "input_type": "search_query",
                },
            )
            if resp.is_error:
                logger.debug(
                    f"Embedding request failed: {resp.status_code} {resp.text[:500]}"
                )
            resp.raise_for_status()
            data = resp.json()
        vector = data["embeddings"]["float"][0]
        logger.debug(f"Received embedding (dimensions={len(vector)})")
        return EmbeddingResult(vector=vector, model=self._model, dimensions=len(vector))
