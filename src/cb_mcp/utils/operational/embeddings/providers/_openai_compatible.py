"""Shared base for the three OpenAI-SDK-backed embedding providers.

OpenAI's ``/v1/embeddings``, Couchbase's Model Service ``/v1/embeddings``
(confirmed against docs.couchbase.com/ai/model-service-api-reference/rest-api.html),
and Voyage's ``/v1/embeddings`` are all close enough to the same shape --
``Authorization: Bearer <key>``, request body ``{"model", "input", ...}``,
response ``data[0].embedding`` -- that the official ``openai`` Python SDK's
client, pointed at each provider's own ``base_url``, talks to all three
correctly. ``extra_body`` carries ``input_type``, the one field outside
OpenAI's own documented schema that Couchbase and Voyage both use.

Cohere is deliberately NOT part of this family despite also being
"OpenAI-compatible" in spirit: its actual wire format differs enough that the
openai SDK can't talk to it (``POST /embed``, not ``/embeddings``; request
key ``texts``, not ``input``; response ``data["embeddings"]["float"][0]``,
not ``data[0].embedding``) -- see cohere.py, which still hand-rolls its
request via httpx.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from openai import OpenAI

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
    #: OpenAI's documented /v1/embeddings schema (model, input,
    #: encoding_format, dimensions, user) has no input_type field -- False by
    #: default. Override to True only for a provider that documents support
    #: for it via extra_body, e.g. Couchbase's Model Service and Voyage.
    _include_input_type: bool = False

    def __init__(self, *, api_key: str, model: str, base_url: str) -> None:
        self._model = model
        self._base_url = base_url
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=30)

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
        extra_body = (
            {"input_type": request.input_type} if self._include_input_type else None
        )
        response = self._client.embeddings.create(
            model=model, input=request.text, extra_body=extra_body
        )
        vector = response.data[0].embedding
        logger.debug(f"Received embedding (dimensions={len(vector)})")
        return EmbeddingResult(
            vector=vector, model=response.model or self._model, dimensions=len(vector)
        )
