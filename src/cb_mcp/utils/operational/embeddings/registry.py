"""The embedding-provider registry: config documentation, factory, and lookup.

This module is the single source of truth for what config each provider
needs. It is deliberately a plain typed Python module, not a JSONL reference
dataset (contrast with ``utils/operational/reference_data.py``, which backs
``discover_tool_input_values``): that format exists for corpora too large to
inline (the metrics dataset is 1140 records) and only needs to be
fuzzy-searched, not structurally consumed. Five providers with a handful of
fields each is the opposite case, and this data drives real logic (field
validation) that a searchable JSONL record can't express — so it is authored
once, here, and read two ways: provider validation (``base.require_fields``)
and README.md/DOCKER.md (hand-mirrored, same as every other doc table in this
repo).

Deliberately NOT rendered into the vector search tools' own docstrings
(``tools/operational/vector_search.py``): which EMBEDDING_* variables a
provider needs is a deployment-time decision an operator makes once via env
vars/CLI flags, not something the calling LLM can act on or change per tool
call -- an operator checking what's actually configured already has
``get_server_configuration_status`` for that. Dumping every provider's full
requirements into every tool call's description would just be unused context.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ....servers.operational.constants import OPERATIONAL_LOGGER_NAMESPACE
from .base import EmbeddingConfigError, EmbeddingProvider

logger = logging.getLogger(f"{OPERATIONAL_LOGGER_NAMESPACE}.utils.embeddings")


@dataclass(frozen=True)
class ConfigField:
    name: str
    settings_key: str
    required: bool
    secret: bool
    description: str


@dataclass(frozen=True)
class ProviderConfigDoc:
    provider_id: str
    display_name: str
    fields: tuple[ConfigField, ...]
    example_model: str


PROVIDER_CONFIG_DOCS: dict[str, ProviderConfigDoc] = {
    "openai": ProviderConfigDoc(
        provider_id="openai",
        display_name="OpenAI (BYOM)",
        fields=(
            ConfigField(
                "EMBEDDING_API_KEY",
                "embedding_api_key",
                required=True,
                secret=True,
                description="OpenAI API key.",
            ),
            ConfigField(
                "EMBEDDING_MODEL",
                "embedding_model",
                required=True,
                secret=False,
                description="Embedding model name, e.g. text-embedding-3-small.",
            ),
            ConfigField(
                "EMBEDDING_ENDPOINT",
                "embedding_endpoint",
                required=False,
                secret=False,
                description="Override base URL to point at an OpenAI-compatible "
                "local server (Ollama/vLLM/LM Studio) instead of api.openai.com.",
            ),
        ),
        example_model="text-embedding-3-small",
    ),
    "couchbase": ProviderConfigDoc(
        provider_id="couchbase",
        display_name="Couchbase Model Service (Provisioned/Serverless)",
        fields=(
            ConfigField(
                "EMBEDDING_API_KEY",
                "embedding_api_key",
                required=True,
                secret=True,
                description="Model Service API key for this deployment.",
            ),
            ConfigField(
                "EMBEDDING_MODEL",
                "embedding_model",
                required=True,
                secret=False,
                description="Deployed model name/reference.",
            ),
            ConfigField(
                "EMBEDDING_ENDPOINT",
                "embedding_endpoint",
                required=True,
                secret=False,
                description="This deployment's base URL, e.g. "
                "https://<id>.ai.couchbase.com — required, no default. Get it "
                "from Capella's AI Data Plane > Models UI.",
            ),
        ),
        example_model="<deployed-model-reference>",
    ),
    "cohere": ProviderConfigDoc(
        provider_id="cohere",
        display_name="Cohere (BYOM)",
        fields=(
            ConfigField(
                "EMBEDDING_API_KEY",
                "embedding_api_key",
                required=True,
                secret=True,
                description="Cohere API key.",
            ),
            ConfigField(
                "EMBEDDING_MODEL",
                "embedding_model",
                required=True,
                secret=False,
                description="Embedding model name, e.g. embed-english-v3.0.",
            ),
            ConfigField(
                "EMBEDDING_ENDPOINT",
                "embedding_endpoint",
                required=False,
                secret=False,
                description="Override base URL, e.g. for a private Cohere deployment.",
            ),
        ),
        example_model="embed-english-v3.0",
    ),
    "voyage": ProviderConfigDoc(
        provider_id="voyage",
        display_name="Voyage AI (BYOM)",
        fields=(
            ConfigField(
                "EMBEDDING_API_KEY",
                "embedding_api_key",
                required=True,
                secret=True,
                description="Voyage API key.",
            ),
            ConfigField(
                "EMBEDDING_MODEL",
                "embedding_model",
                required=True,
                secret=False,
                description="Embedding model name, e.g. voyage-3.",
            ),
            ConfigField(
                "EMBEDDING_ENDPOINT",
                "embedding_endpoint",
                required=False,
                secret=False,
                description="Override base URL.",
            ),
        ),
        example_model="voyage-3",
    ),
    "bedrock": ProviderConfigDoc(
        provider_id="bedrock",
        display_name="Amazon Bedrock (BYOM)",
        fields=(
            ConfigField(
                "EMBEDDING_MODEL",
                "embedding_model",
                required=True,
                secret=False,
                description="Bedrock model ID, e.g. amazon.titan-embed-text-v2:0 "
                "(Titan embedding family only).",
            ),
            ConfigField(
                "EMBEDDING_AWS_ACCESS_KEY_ID",
                "embedding_aws_access_key_id",
                required=False,
                secret=True,
                description="Omit to use the default AWS credential chain "
                "(env vars, ~/.aws/credentials, instance role).",
            ),
            ConfigField(
                "EMBEDDING_AWS_SECRET_ACCESS_KEY",
                "embedding_aws_secret_access_key",
                required=False,
                secret=True,
                description="Paired with the access key ID.",
            ),
            ConfigField(
                "EMBEDDING_AWS_REGION",
                "embedding_aws_region",
                required=False,
                secret=False,
                description="Falls back to the AWS SDK's own region resolution if unset.",
            ),
        ),
        example_model="amazon.titan-embed-text-v2:0",
    ),
}


def _load_provider_classes() -> dict[str, type[EmbeddingProvider]]:
    """Import provider classes lazily so an unconfigured deployment pays
    nothing — in particular, so ``bedrock.py``'s ``import boto3`` never runs
    in a process that never calls these tools.
    """
    from .providers.bedrock import BedrockEmbeddingProvider  # noqa: PLC0415
    from .providers.cohere import CohereEmbeddingProvider  # noqa: PLC0415
    from .providers.couchbase_provisioned import (  # noqa: PLC0415
        CouchbaseProvisionedEmbeddingProvider,
    )
    from .providers.openai import OpenAIEmbeddingProvider  # noqa: PLC0415
    from .providers.voyage import VoyageEmbeddingProvider  # noqa: PLC0415

    return {
        "openai": OpenAIEmbeddingProvider,
        "couchbase": CouchbaseProvisionedEmbeddingProvider,
        "cohere": CohereEmbeddingProvider,
        "voyage": VoyageEmbeddingProvider,
        "bedrock": BedrockEmbeddingProvider,
    }


def resolve_embedding_provider(settings: Mapping[str, Any]) -> EmbeddingProvider:
    """The single entry point tools call. Reads ``settings``, never env vars."""
    provider_id = (settings.get("embedding_provider") or "").strip().lower()
    providers = _load_provider_classes()
    if not provider_id:
        raise EmbeddingConfigError(
            "EMBEDDING_PROVIDER is not configured. Set it to one of: "
            f"{', '.join(sorted(providers))}."
        )
    provider_cls = providers.get(provider_id)
    if provider_cls is None:
        raise EmbeddingConfigError(
            f"Unknown EMBEDDING_PROVIDER {provider_id!r}. Must be one of: "
            f"{', '.join(sorted(providers))}."
        )
    logger.debug(
        f"Resolved embedding provider {provider_id!r} -> {provider_cls.__name__}"
    )
    return provider_cls.from_settings(settings)
