"""Accuracy tests for the Operational Insights metadata tools.

Covers:
  - oi_get_databases_in_cluster
  - oi_get_scopes_in_database
  - oi_get_collections_in_scope
  - oi_get_schema_for_collection
  - get_server_configuration_status (registered by this server too, under the
    same name the operational server uses)

The discriminating risk these cases guard against is cross-server confusion:
the model has operational-server habits (buckets, ``get_buckets_in_cluster``,
``get_schema_for_collection``) and the OI tools are deliberately named close
enough to be mistaken for them. Several prompts below therefore use the word
"bucket"-free phrasing a user would actually type ("what databases are
there?") and assert the ``oi_``-prefixed tool is the one selected.
"""

from __future__ import annotations

import json

import pytest

from accuracy.sdk import (
    AccuracyCase,
    DiskResultStorage,
    Matcher,
    OpenAIAgent,
    run_accuracy_case,
)
from accuracy.sdk.types import ExpectedToolCall


def _build_cases(
    database: str,
    scope: str,
    schema_keyspace: tuple[str, str, str],
) -> list[AccuracyCase]:
    cases: list[AccuracyCase] = [
        AccuracyCase(
            test_id="oi_get_databases_in_cluster",
            prompt="What databases exist on this cluster? List them.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_get_databases_in_cluster",
                    parameters={},
                ),
            ],
        ),
        AccuracyCase(
            test_id="oi_get_scopes_in_database",
            prompt=f"List the scopes in the '{database}' database.",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_get_scopes_in_database",
                    parameters={"database_name": database},
                ),
            ],
        ),
        AccuracyCase(
            test_id="oi_get_collections_in_scope",
            prompt=(
                f"What collections are in scope '{scope}' of database "
                f"'{database}'?"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_get_collections_in_scope",
                    parameters={
                        "database_name": database,
                        "scope_name": scope,
                    },
                ),
            ],
        ),
        AccuracyCase(
            test_id="get_server_configuration_status",
            prompt=(
                "How is this MCP server configured right now — is it in "
                "read-only mode, and what is it connected to?"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="get_server_configuration_status",
                    parameters=Matcher.empty_object_or_undefined(),
                ),
            ],
        ),
        # Phrased the way a user explores an unfamiliar cluster, with no tool
        # vocabulary at all — checks the model reaches for the OI lister
        # rather than an operational-server equivalent it cannot see.
        AccuracyCase(
            test_id="conversational_whats_on_this_cluster",
            prompt="I just connected to this cluster. What data is on it?",
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_get_databases_in_cluster",
                    parameters=Matcher.any_value(),
                ),
            ],
        ),
    ]

    # Schema inference samples real document content, so these two run
    # against a collection that already holds data — travel-sample's
    # inventory.airline unless CB_OI_TEST_COLLECTION points elsewhere.
    schema_db, schema_scope, schema_collection = schema_keyspace

    cases.append(
        AccuracyCase(
            test_id="oi_get_schema_for_collection",
            prompt=(
                f"What is the schema / document structure of collection "
                f"'{schema_collection}' in scope '{schema_scope}' of database "
                f"'{schema_db}'? Infer it from the documents."
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_get_schema_for_collection",
                    parameters={
                        "database_name": schema_db,
                        "scope_name": schema_scope,
                        "collection_name": schema_collection,
                        # sample_size / num_sample_values both default, so
                        # the model is free to omit them or pass the
                        # documented defaults.
                        "sample_size": Matcher.any_of(
                            Matcher.undefined(), Matcher.number()
                        ),
                        "num_sample_values": Matcher.any_of(
                            Matcher.undefined(), Matcher.number()
                        ),
                    },
                ),
            ],
        )
    )
    cases.append(
        AccuracyCase(
            test_id="conversational_what_fields_do_docs_have",
            prompt=(
                f"I'm new to the '{schema_collection}' collection in scope "
                f"'{schema_scope}' of database '{schema_db}'. What fields do "
                "its documents have?"
            ),
            expected_tools=[
                ExpectedToolCall(
                    tool_name="oi_get_schema_for_collection",
                    parameters=Matcher.any_value(),
                ),
            ],
        )
    )

    return cases


@pytest.fixture()
def metadata_cases(
    oi_database: str, oi_scope: str, schema_keyspace: tuple[str, str, str]
):
    return _build_cases(oi_database, oi_scope, schema_keyspace)


# Schema cases are split into their own test so they can run against a
# different keyspace (one holding real documents) than the metadata cases.
METADATA_CASE_IDS = [
    "oi_get_databases_in_cluster",
    "oi_get_scopes_in_database",
    "oi_get_collections_in_scope",
    "get_server_configuration_status",
    "conversational_whats_on_this_cluster",
]

COLLECTION_CASE_IDS = [
    "oi_get_schema_for_collection",
    "conversational_what_fields_do_docs_have",
]


async def _run_case(
    case_id: str,
    cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    case = next(c for c in cases if c.test_id == case_id)
    result = await run_accuracy_case(
        case,
        accuracy_client_factory=accuracy_client,
        openai_agent=openai_agent,
        openai_model=openai_model,
        result_storage=result_storage,
        accuracy_run_id=accuracy_run_id,
        commit_sha=commit_sha,
    )

    assert result.accuracy >= 0.75, (
        f"Accuracy for case '{case_id}' was {result.accuracy}. "
        f"Expected: {case.expected_tools}. "
        f"Actual: {json.dumps([c.__dict__ for c in result.actual_calls], indent=2, default=str)}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", METADATA_CASE_IDS)
async def test_oi_metadata_tool_accuracy(
    case_id: str,
    metadata_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    await _run_case(
        case_id,
        metadata_cases,
        accuracy_client,
        openai_agent,
        openai_model,
        result_storage,
        accuracy_run_id,
        commit_sha,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", COLLECTION_CASE_IDS)
async def test_oi_schema_tool_accuracy(
    case_id: str,
    metadata_cases: list[AccuracyCase],
    accuracy_client,
    openai_agent: OpenAIAgent,
    openai_model: str,
    result_storage: DiskResultStorage,
    accuracy_run_id: str,
    commit_sha: str,
) -> None:
    await _run_case(
        case_id,
        metadata_cases,
        accuracy_client,
        openai_agent,
        openai_model,
        result_storage,
        accuracy_run_id,
        commit_sha,
    )
