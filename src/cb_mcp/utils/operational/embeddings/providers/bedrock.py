"""Amazon Bedrock embedding provider (BYOM).

Requires the optional ``bedrock-embeddings`` extra
(``pip install couchbase-mcp-server[bedrock-embeddings]``) for ``boto3`` --
imported lazily here, never at module scope, so a process that
never selects ``EMBEDDING_PROVIDER=bedrock`` (and in particular, one that
doesn't have the extra installed) never pays for or needs the import.

Scoped to the Titan embedding family's request/response shape (``inputText``
in, ``embedding`` out) — matches the product spec's own example model
(``amazon.titan-embed-text-v2:0``). Cohere-on-Bedrock uses a different request
body (``{"texts": [...], "input_type": ...}``) and is not supported here; a
follow-up can branch on model-ID prefix if that's needed later.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from .....servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from ..base import (
    EmbeddingConfigError,
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResult,
    require_fields,
)
from ..registry import PROVIDER_CONFIG_DOCS, ProviderConfigDoc

logger = logging.getLogger(
    f"{OPERATIONAL_LOGGER_NAMESPACE}.utils.embeddings.providers.bedrock"
)


class BedrockEmbeddingProvider(EmbeddingProvider):
    provider_id = "bedrock"

    def __init__(self, *, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    @classmethod
    def describe_config(cls) -> ProviderConfigDoc:
        return PROVIDER_CONFIG_DOCS[cls.provider_id]

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> BedrockEmbeddingProvider:
        fields = require_fields(settings, cls.describe_config())
        try:
            import boto3  # noqa: PLC0415
        except ImportError as e:
            raise EmbeddingConfigError(
                "Bedrock support requires the 'bedrock-embeddings' extra: "
                "pip install couchbase-mcp-server[bedrock-embeddings]"
            ) from e
        # Falsy/None credential kwargs fall through to boto3's own default
        # credential chain (env vars, ~/.aws/credentials, instance role) —
        # EMBEDDING_AWS_* are overrides, not requirements.
        region = settings.get("embedding_aws_region") or None
        logger.debug(
            f"Configuring Bedrock client (region={region!r}, "
            f"explicit_credentials={bool(settings.get('embedding_aws_access_key_id'))})"
        )
        client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=settings.get("embedding_aws_access_key_id") or None,
            aws_secret_access_key=settings.get("embedding_aws_secret_access_key")
            or None,
        )
        return cls(client=client, model=fields["embedding_model"])

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        logger.debug(f"Invoking Bedrock model {self._model!r}")
        response = self._client.invoke_model(
            modelId=self._model,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({"inputText": request.text}),
        )
        body = json.loads(response["body"].read())
        vector = body["embedding"]
        logger.debug(f"Received embedding (dimensions={len(vector)})")
        return EmbeddingResult(vector=vector, model=self._model, dimensions=len(vector))
