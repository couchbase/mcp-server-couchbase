"""Unit tests for the embedding-provider abstraction
(cb_mcp.utils.operational.embeddings).

Covers:
- registry.resolve_embedding_provider: unset/unknown EMBEDDING_PROVIDER,
  case-insensitive matching, dispatch to the right provider class.
- base.require_fields: missing-required-field error naming every field at
  once, optional fields passing through as None when absent.
- Each BYOM/managed provider's from_settings (endpoint defaulting/requiring)
  and embed() request/response handling, with httpx.Client mocked so no
  network call is ever made.
- bedrock: the "extra not installed" path (real in this environment, since
  boto3 is an optional extra) and, separately, the happy path with a faked
  boto3 module so the request/response shape is exercised too.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cb_mcp.utils.operational.embeddings import EmbeddingConfigError
from cb_mcp.utils.operational.embeddings.base import EmbeddingRequest, require_fields
from cb_mcp.utils.operational.embeddings.providers.bedrock import (
    BedrockEmbeddingProvider,
)
from cb_mcp.utils.operational.embeddings.providers.cohere import CohereEmbeddingProvider
from cb_mcp.utils.operational.embeddings.providers.couchbase_provisioned import (
    CouchbaseProvisionedEmbeddingProvider,
)
from cb_mcp.utils.operational.embeddings.providers.openai import OpenAIEmbeddingProvider
from cb_mcp.utils.operational.embeddings.providers.voyage import VoyageEmbeddingProvider
from cb_mcp.utils.operational.embeddings.registry import resolve_embedding_provider


def _mock_httpx_client(module_path: str, json_body: dict):
    """Patch ``httpx.Client`` in ``module_path`` so ``.post(...)`` returns a
    fake response with the given JSON body, and no real network call happens.
    Returns the mocked client instance so callers can assert on .post's args.
    """
    response = MagicMock()
    response.json.return_value = json_body
    client_instance = MagicMock()
    client_instance.post.return_value = response
    client_cm = MagicMock()
    client_cm.__enter__.return_value = client_instance
    return patch(f"{module_path}.httpx.Client", return_value=client_cm), client_instance


class TestRequireFields:
    def test_missing_required_fields_named_all_at_once(self) -> None:
        doc = OpenAIEmbeddingProvider.describe_config()
        with pytest.raises(EmbeddingConfigError) as exc_info:
            require_fields({}, doc)
        assert "EMBEDDING_API_KEY" in str(exc_info.value)
        assert "EMBEDDING_MODEL" in str(exc_info.value)

    def test_optional_field_absent_is_none_not_an_error(self) -> None:
        doc = OpenAIEmbeddingProvider.describe_config()
        values = require_fields(
            {"embedding_api_key": "sk-x", "embedding_model": "m"}, doc
        )
        assert values["embedding_endpoint"] is None


class TestResolveEmbeddingProvider:
    def test_unset_provider_is_config_error(self) -> None:
        with pytest.raises(EmbeddingConfigError, match="not configured"):
            resolve_embedding_provider({})

    def test_unknown_provider_is_config_error(self) -> None:
        with pytest.raises(EmbeddingConfigError, match="Unknown EMBEDDING_PROVIDER"):
            resolve_embedding_provider({"embedding_provider": "not-a-provider"})

    def test_dispatches_case_insensitively(self) -> None:
        provider = resolve_embedding_provider(
            {
                "embedding_provider": "OpenAI",
                "embedding_api_key": "sk-x",
                "embedding_model": "text-embedding-3-small",
            }
        )
        assert isinstance(provider, OpenAIEmbeddingProvider)


class TestOpenAIProvider:
    def test_defaults_to_public_base_url(self) -> None:
        provider = OpenAIEmbeddingProvider.from_settings(
            {"embedding_api_key": "sk-x", "embedding_model": "text-embedding-3-small"}
        )
        assert provider._base_url == "https://api.openai.com/v1"

    def test_embedding_endpoint_overrides_base_url(self) -> None:
        provider = OpenAIEmbeddingProvider.from_settings(
            {
                "embedding_api_key": "ollama",
                "embedding_model": "nomic-embed-text",
                "embedding_endpoint": "http://localhost:11434/v1/",
            }
        )
        assert provider._base_url == "http://localhost:11434/v1"

    def test_embed_posts_and_parses_response(self) -> None:
        provider = OpenAIEmbeddingProvider.from_settings(
            {"embedding_api_key": "sk-x", "embedding_model": "text-embedding-3-small"}
        )
        patcher, client = _mock_httpx_client(
            "cb_mcp.utils.operational.embeddings.providers._openai_compatible",
            {
                "data": [{"embedding": [0.1, 0.2, 0.3]}],
                "model": "text-embedding-3-small",
            },
        )
        with patcher:
            result = provider.embed(
                EmbeddingRequest(text="hello", model="text-embedding-3-small")
            )

        client.post.assert_called_once()
        url, kwargs = client.post.call_args
        assert url[0] == "https://api.openai.com/v1/embeddings"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-x"
        assert kwargs["json"]["input"] == "hello"
        assert kwargs["json"]["input_type"] == "query"
        assert result.vector == [0.1, 0.2, 0.3]
        assert result.dimensions == 3


class TestCouchbaseProvisionedProvider:
    def test_endpoint_is_required(self) -> None:
        with pytest.raises(EmbeddingConfigError, match="EMBEDDING_ENDPOINT"):
            CouchbaseProvisionedEmbeddingProvider.from_settings(
                {"embedding_api_key": "k", "embedding_model": "m"}
            )

    def test_endpoint_configured_succeeds(self) -> None:
        provider = CouchbaseProvisionedEmbeddingProvider.from_settings(
            {
                "embedding_api_key": "k",
                "embedding_model": "m",
                "embedding_endpoint": "https://abc123.ai.couchbase.com",
            }
        )
        assert provider._base_url == "https://abc123.ai.couchbase.com"


class TestCohereProvider:
    def test_embed_uses_v2_embed_endpoint_and_search_query_input_type(self) -> None:
        provider = CohereEmbeddingProvider.from_settings(
            {"embedding_api_key": "k", "embedding_model": "embed-english-v3.0"}
        )
        patcher, client = _mock_httpx_client(
            "cb_mcp.utils.operational.embeddings.providers.cohere",
            {"embeddings": {"float": [[0.4, 0.5]]}},
        )
        with patcher:
            result = provider.embed(
                EmbeddingRequest(text="hello", model="embed-english-v3.0")
            )

        url, kwargs = client.post.call_args
        assert url[0] == "https://api.cohere.com/v2/embed"
        assert kwargs["json"]["texts"] == ["hello"]
        assert kwargs["json"]["input_type"] == "search_query"
        assert result.vector == [0.4, 0.5]


class TestVoyageProvider:
    def test_embed_uses_embeddings_endpoint(self) -> None:
        provider = VoyageEmbeddingProvider.from_settings(
            {"embedding_api_key": "k", "embedding_model": "voyage-3"}
        )
        patcher, client = _mock_httpx_client(
            "cb_mcp.utils.operational.embeddings.providers.voyage",
            {"data": [{"embedding": [0.6, 0.7]}]},
        )
        with patcher:
            result = provider.embed(EmbeddingRequest(text="hello", model="voyage-3"))

        url, kwargs = client.post.call_args
        assert url[0] == "https://api.voyageai.com/v1/embeddings"
        assert kwargs["json"]["input"] == ["hello"]
        assert result.vector == [0.6, 0.7]


class TestBedrockProvider:
    def test_missing_boto3_is_actionable_config_error(self) -> None:
        with (
            patch.dict(sys.modules, {"boto3": None}),
            pytest.raises(EmbeddingConfigError, match=r"\[bedrock\]"),
        ):
            BedrockEmbeddingProvider.from_settings(
                {"embedding_model": "amazon.titan-embed-text-v2:0"}
            )

    def test_happy_path_with_faked_boto3(self) -> None:
        fake_client = MagicMock()
        response_body = MagicMock()
        response_body.read.return_value = b'{"embedding": [0.1, 0.2, 0.3]}'
        fake_client.invoke_model.return_value = {"body": response_body}

        fake_boto3 = SimpleNamespace(client=MagicMock(return_value=fake_client))

        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            provider = BedrockEmbeddingProvider.from_settings(
                {
                    "embedding_model": "amazon.titan-embed-text-v2:0",
                    "embedding_aws_region": "us-east-1",
                }
            )
            result = provider.embed(
                EmbeddingRequest(text="hello", model="amazon.titan-embed-text-v2:0")
            )

        fake_boto3.client.assert_called_once_with(
            "bedrock-runtime",
            region_name="us-east-1",
            aws_access_key_id=None,
            aws_secret_access_key=None,
        )
        fake_client.invoke_model.assert_called_once()
        assert fake_client.invoke_model.call_args.kwargs["modelId"] == (
            "amazon.titan-embed-text-v2:0"
        )
        assert result.vector == [0.1, 0.2, 0.3]
