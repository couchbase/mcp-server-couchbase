"""Unit tests for the embedding-provider abstraction
(cb_mcp.utils.operational.embeddings).

Covers:
- registry.resolve_embedding_provider: unset/unknown EMBEDDING_PROVIDER,
  case-insensitive matching, dispatch to the right provider class.
- base.require_fields: missing-required-field error naming every field at
  once, optional fields passing through as None when absent.
- Each BYOM/managed provider's from_settings (endpoint defaulting/requiring)
  and embed() request/response handling. openai/couchbase/voyage share
  _OpenAICompatibleProvider (backed by the openai SDK) and are tested via a
  faked OpenAI class so no network call is ever made; cohere still hand-rolls
  its request via httpx (see _openai_compatible.py's module docstring for why
  it isn't part of that family) and is tested the same way it always was.
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
    Only cohere.py still uses this -- see _mock_openai_client below for the
    openai/couchbase/voyage family, which goes through the openai SDK.
    """
    response = MagicMock()
    response.json.return_value = json_body
    client_instance = MagicMock()
    client_instance.post.return_value = response
    client_cm = MagicMock()
    client_cm.__enter__.return_value = client_instance
    return patch(f"{module_path}.httpx.Client", return_value=client_cm), client_instance


_OPENAI_COMPATIBLE_MODULE = (
    "cb_mcp.utils.operational.embeddings.providers._openai_compatible"
)


def _mock_openai_client(vector: list[float], model: str | None = None):
    """Patch the openai SDK's ``OpenAI`` class (as imported into
    _openai_compatible.py, shared by the openai/couchbase/voyage providers)
    so constructing a client returns a fake whose .embeddings.create(...)
    returns a fake response exposing .data[0].embedding and .model --
    mimicking openai.types.CreateEmbeddingResponse's shape without a real
    network call. Returns (patcher, mock_openai_class, create_mock) so
    callers can assert on both the client's construction kwargs (api_key,
    base_url) and the create() call's kwargs (model, input, extra_body).
    """
    response = SimpleNamespace(data=[SimpleNamespace(embedding=vector)], model=model)
    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = response
    mock_openai_class = MagicMock(return_value=fake_client)
    return (
        patch(f"{_OPENAI_COMPATIBLE_MODULE}.OpenAI", mock_openai_class),
        mock_openai_class,
        fake_client.embeddings.create,
    )


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

    def test_embed_calls_sdk_and_parses_response(self) -> None:
        # from_settings() constructs the openai SDK client eagerly (in
        # __init__), so it must happen inside the patch context too --
        # patching OpenAI after construction wouldn't affect an
        # already-built client instance.
        patcher, mock_openai_class, create = _mock_openai_client(
            vector=[0.1, 0.2, 0.3], model="text-embedding-3-small"
        )
        with patcher:
            provider = OpenAIEmbeddingProvider.from_settings(
                {
                    "embedding_api_key": "sk-x",
                    "embedding_model": "text-embedding-3-small",
                }
            )
            result = provider.embed(
                EmbeddingRequest(text="hello", model="text-embedding-3-small")
            )

        mock_openai_class.assert_called_once_with(
            api_key="sk-x", base_url="https://api.openai.com/v1", timeout=30
        )
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        assert kwargs["model"] == "text-embedding-3-small"
        assert kwargs["input"] == "hello"
        # OpenAI's real /v1/embeddings schema has no input_type field --
        # must not be sent, unlike Couchbase's Model Service and Voyage (see
        # TestCouchbaseProvisionedProvider.test_embed_includes_input_type).
        assert kwargs["extra_body"] is None
        assert result.vector == [0.1, 0.2, 0.3]
        assert result.dimensions == 3


class TestCouchbaseProvisionedProvider:
    def test_endpoint_is_required(self) -> None:
        with pytest.raises(EmbeddingConfigError, match="EMBEDDING_ENDPOINT"):
            CouchbaseProvisionedEmbeddingProvider.from_settings(
                {"embedding_api_key": "k", "embedding_model": "m"}
            )

    def test_bare_deployment_host_gets_v1_appended(self) -> None:
        """The host Capella's UI shows has no /v1 -- the actual API lives
        under /v1/embeddings, so it must be appended, or every real call
        hits the wrong route."""
        provider = CouchbaseProvisionedEmbeddingProvider.from_settings(
            {
                "embedding_api_key": "k",
                "embedding_model": "m",
                "embedding_endpoint": "https://abc123.ai.couchbase.com",
            }
        )
        assert provider._base_url == "https://abc123.ai.couchbase.com/v1"

    def test_endpoint_already_ending_in_v1_is_not_duplicated(self) -> None:
        provider = CouchbaseProvisionedEmbeddingProvider.from_settings(
            {
                "embedding_api_key": "k",
                "embedding_model": "m",
                "embedding_endpoint": "https://abc123.ai.couchbase.com/v1",
            }
        )
        assert provider._base_url == "https://abc123.ai.couchbase.com/v1"

    def test_embed_uses_v1_base_url_and_includes_input_type(self) -> None:
        """Proves the fix end-to-end: the openai SDK client is constructed
        with the /v1-appended base_url, for the exact bare-host
        configuration Capella's UI gives an operator (the SDK's own request
        building handles joining /embeddings onto it from there). Also:
        unlike OpenAI's real API, Couchbase's Model Service documents
        input_type as a real optional field -- must be sent via extra_body,
        the opposite of TestOpenAIProvider.test_embed_calls_sdk_and_parses_response.
        """
        patcher, mock_openai_class, create = _mock_openai_client(
            vector=[0.1, 0.2], model="m"
        )
        with patcher:
            provider = CouchbaseProvisionedEmbeddingProvider.from_settings(
                {
                    "embedding_api_key": "k",
                    "embedding_model": "m",
                    "embedding_endpoint": "https://abc123.ai.couchbase.com",
                }
            )
            provider.embed(
                EmbeddingRequest(text="hello", model="m", input_type="query")
            )

        assert (
            mock_openai_class.call_args.kwargs["base_url"]
            == "https://abc123.ai.couchbase.com/v1"
        )
        assert create.call_args.kwargs["extra_body"] == {"input_type": "query"}


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
    def test_defaults_to_public_base_url(self) -> None:
        provider = VoyageEmbeddingProvider.from_settings(
            {"embedding_api_key": "k", "embedding_model": "voyage-3"}
        )
        assert provider._base_url == "https://api.voyageai.com/v1"

    def test_embed_calls_sdk_and_includes_input_type(self) -> None:
        """Voyage's /v1/embeddings documents input as either a string or a
        list of strings, so the shared base's bare-string input (matching
        OpenAI's own schema) is valid here too -- confirmed against Voyage's
        own API reference, not assumed. Like Couchbase, Voyage documents
        input_type as a real field outside OpenAI's own schema -- sent via
        extra_body, same mechanism as
        TestCouchbaseProvisionedProvider.test_embed_uses_v1_base_url_and_includes_input_type.
        """
        patcher, mock_openai_class, create = _mock_openai_client(vector=[0.6, 0.7])
        with patcher:
            provider = VoyageEmbeddingProvider.from_settings(
                {"embedding_api_key": "k", "embedding_model": "voyage-3"}
            )
            result = provider.embed(EmbeddingRequest(text="hello", model="voyage-3"))

        mock_openai_class.assert_called_once_with(
            api_key="k", base_url="https://api.voyageai.com/v1", timeout=30
        )
        kwargs = create.call_args.kwargs
        assert kwargs["input"] == "hello"
        assert kwargs["extra_body"] == {"input_type": "query"}
        assert result.vector == [0.6, 0.7]


class TestBedrockProvider:
    def test_missing_boto3_is_actionable_config_error(self) -> None:
        with (
            patch.dict(sys.modules, {"boto3": None}),
            pytest.raises(EmbeddingConfigError, match=r"\[bedrock-embeddings\]"),
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
