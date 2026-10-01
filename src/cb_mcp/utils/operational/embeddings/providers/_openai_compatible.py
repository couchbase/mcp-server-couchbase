"""Shared base for the two OpenAI-shaped REST embedding providers.

OpenAI's ``/v1/embeddings`` and Couchbase's Model Service ``/v1/embeddings``
(confirmed against docs.couchbase.com/ai/model-service-api-reference/rest-api.html)
are identical in shape: ``Authorization: Bearer <key>``, request body
``{"model", "input", "input_type"}``, response ``data[0].embedding``. This base
class holds that logic once; the two subclasses differ only in whether
``EMBEDDING_ENDPOINT`` has a public default.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from .....servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ..base import (
    EmbeddingConfigError,
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResult,
    require_fields,
)

logger = logging.getLogger(
    f"{OPERATIONAL_LOGGER_NAMESPACE}.utils.embeddings.providers.openai_compatible"
)


class _OpenAICompatibleProvider(EmbeddingProvider):
    #: None means EMBEDDING_ENDPOINT is required (no public shared domain).
    _default_base_url: str | None = None

    def __init__(self, *, api_key: str, model: str, base_url: str) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url

    @classmethod
    def _normalize_base_url(cls, base_url: str) -> str:
        """Hook for a subclass whose operator-supplied endpoint needs a fixed
        suffix appended -- e.g. Couchbase's bare deployment host needs /v1,
        see couchbase_provisioned.py. No-op by default: OpenAI and
        OpenAI-compatible local servers already expect the operator to supply
        the full path, /v1 included, matching the documented examples.
        """
        return base_url

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> _OpenAICompatibleProvider:
        fields = require_fields(settings, cls.describe_config())
        base_url = fields.get("embedding_endpoint") or cls._default_base_url
        if not base_url:
            raise EmbeddingConfigError(
                f"EMBEDDING_ENDPOINT is required for provider {cls.provider_id!r}."
            )
        base_url = cls._normalize_base_url(base_url.rstrip("/"))
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
                    "input": request.text,
                    "input_type": request.input_type,
                },
            )
            if resp.is_error:
                # The exception raise_for_status() throws carries the status
                # line but not the response body, which is usually where the
                # provider actually explains what went wrong (bad model name,
                # invalid key, ...). Logged here, at DEBUG, since the caller's
                # except block already logs the exception itself at ERROR.
                logger.debug(
                    f"Embedding request failed: {resp.status_code} {resp.text[:500]}"
                )
            resp.raise_for_status()
            data = resp.json()
        vector = data["data"][0]["embedding"]
        logger.debug(f"Received embedding (dimensions={len(vector)})")
        return EmbeddingResult(
            vector=vector, model=data.get("model", self._model), dimensions=len(vector)
        )
