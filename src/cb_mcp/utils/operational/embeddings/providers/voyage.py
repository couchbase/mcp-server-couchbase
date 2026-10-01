"""Voyage AI embedding provider (BYOM)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from .....servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ..base import EmbeddingProvider, EmbeddingRequest, EmbeddingResult, require_fields
from ..registry import PROVIDER_CONFIG_DOCS, ProviderConfigDoc

logger = logging.getLogger(
    f"{OPERATIONAL_LOGGER_NAMESPACE}.utils.embeddings.providers.voyage"
)

_DEFAULT_BASE_URL = "https://api.voyageai.com/v1"


class VoyageEmbeddingProvider(EmbeddingProvider):
    provider_id = "voyage"

    def __init__(self, *, api_key: str, model: str, base_url: str) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url

    @classmethod
    def describe_config(cls) -> ProviderConfigDoc:
        return PROVIDER_CONFIG_DOCS[cls.provider_id]

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> VoyageEmbeddingProvider:
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
            f"Requesting embedding from {self._base_url}/embeddings (model={model!r})"
        )
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": model,
                    "input": [request.text],
                    "input_type": "query",
                },
            )
            if resp.is_error:
                logger.debug(
                    f"Embedding request failed: {resp.status_code} {resp.text[:500]}"
                )
            resp.raise_for_status()
            data = resp.json()
        vector = data["data"][0]["embedding"]
        logger.debug(f"Received embedding (dimensions={len(vector)})")
        return EmbeddingResult(vector=vector, model=self._model, dimensions=len(vector))
