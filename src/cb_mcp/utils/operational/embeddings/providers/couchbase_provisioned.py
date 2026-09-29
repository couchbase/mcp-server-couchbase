"""Couchbase Model Service (Provisioned) embedding provider — managed, first-class.

Contract confirmed against docs.couchbase.com/ai/api-guide/api-use.html and
docs.couchbase.com/ai/model-service-api-reference/rest-api.html
(``createEmbedding`` operation): ``POST {deployment-base-url}/v1/embeddings``,
``Authorization: Bearer <key>``, body ``{model, input, input_type}``, response
``data[0].embedding`` — identical in shape to OpenAI's ``/v1/embeddings``.

There is no public shared Couchbase-hosted domain (unlike api.openai.com) —
each deployment gets its own base URL (e.g. ``https://<id>.ai.couchbase.com``),
so ``EMBEDDING_ENDPOINT`` is required for this provider, not optional.
"""

from __future__ import annotations

from ..registry import PROVIDER_CONFIG_DOCS, ProviderConfigDoc
from ._openai_compatible import _OpenAICompatibleProvider


class CouchbaseProvisionedEmbeddingProvider(_OpenAICompatibleProvider):
    provider_id = "couchbase"
    _default_base_url = None  # EMBEDDING_ENDPOINT is required — see module docstring

    @classmethod
    def describe_config(cls) -> ProviderConfigDoc:
        return PROVIDER_CONFIG_DOCS[cls.provider_id]
