"""SDK for AI-in-the-loop accuracy testing of the Couchbase MCP Server."""

from .agent import OpenAIAgent
from .client import AccuracyTestingClient
from .judge import JudgeVerdict, LLMJudge
from .matcher import Matcher
from .result_storage import DiskResultStorage
from .runner import (
    AccuracyCase,
    AccuracyCaseResult,
    ResultCase,
    ResultCaseResult,
    extract_tool_results,
    run_accuracy_case,
    run_result_case,
)
from .scorer import calculate_tool_calling_accuracy
from .seeding import (
    delete_document,
    doc_id,
    drop_scope,
    seed_collection,
    seed_document,
    seed_scope,
    unique_name,
)
from .types import ExpectedToolCall, LLMToolCall, ModelResponse, PromptResult

__all__ = [
    "AccuracyCase",
    "AccuracyCaseResult",
    "AccuracyTestingClient",
    "DiskResultStorage",
    "ExpectedToolCall",
    "JudgeVerdict",
    "LLMJudge",
    "LLMToolCall",
    "Matcher",
    "ModelResponse",
    "OpenAIAgent",
    "PromptResult",
    "ResultCase",
    "ResultCaseResult",
    "calculate_tool_calling_accuracy",
    "delete_document",
    "doc_id",
    "drop_scope",
    "extract_tool_results",
    "run_accuracy_case",
    "run_result_case",
    "seed_collection",
    "seed_document",
    "seed_scope",
    "unique_name",
]
