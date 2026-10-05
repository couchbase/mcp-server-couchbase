"""Pluggable embedding-provider abstraction for the vector search tools.

Turns query text into a vector via a configured provider — Couchbase's own
Model Service, or a bring-your-own-model backend (OpenAI, Cohere, Voyage,
Bedrock) — selected by the ``EMBEDDING_PROVIDER`` setting. See
:mod:`cb_mcp.tools.operational.vector_search` for the tools that use this, and
``registry.PROVIDER_CONFIG_DOCS`` for the exact config each provider needs.
"""

from .base import (
    EmbeddingConfigError,
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResult,
)
from .registry import embed_query_text, resolve_embedding_provider

__all__ = [
    "EmbeddingConfigError",
    "EmbeddingProvider",
    "EmbeddingRequest",
    "EmbeddingResult",
    "embed_query_text",
    "resolve_embedding_provider",
]
