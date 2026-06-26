"""Fixtures shared by all accuracy tests.

Accuracy tests run an OpenAI-driven agent against the real MCP server and
score the resulting tool calls. To keep accidental runs from spinning up
LLM bills, every test in this directory is skipped unless both
``OPENAI_API_KEY`` (or ``CB_ACCURACY_OPENAI_API_KEY``) and the Couchbase
demo cluster env vars are set.

Environment variables:
  - ``CB_ACCURACY_OPENAI_API_KEY`` / ``OPENAI_API_KEY`` — required.
  - ``CB_ACCURACY_OPENAI_MODEL`` — defaults to ``gpt-4o``.
  - ``CB_ACCURACY_OPENAI_BASE_URL`` — optional override (Azure, proxy, etc.).
  - ``CB_ACCURACY_RUN_ID`` — run identifier; defaults to ``local-<unix_ts>``.
  - ``CB_ACCURACY_RESULTS_DIR`` — output dir; defaults to
    ``tests/accuracy/results``.
  - ``CB_CONNECTION_STRING`` / ``CB_USERNAME`` / ``CB_PASSWORD`` — required
    for the underlying MCP server to talk to Couchbase.
  - ``CB_MCP_TEST_BUCKET`` / ``CB_MCP_TEST_SCOPE`` / ``CB_MCP_TEST_COLLECTION``
    — the bucket/scope/collection used in accuracy prompts.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from _test_env import (
    _build_env,
    get_test_collection,
    get_test_scope,
    require_test_bucket,
)
from mcp import ClientSession, StdioServerParameters, stdio_client

from accuracy.sdk import (
    AccuracyTestingClient,
    DiskResultStorage,
    LLMJudge,
    OpenAIAgent,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMEOUT = int(os.getenv("CB_ACCURACY_TIMEOUT", "300"))

_ACCURACY_DIR = Path(__file__).resolve().parent
_RESULT_VALIDATION_DIR = _ACCURACY_DIR / "result_validation"


def pytest_collection_modifyitems(config, items):
    """Auto-tag accuracy tests by directory so test files stay decorator-free.

    Everything under tests/accuracy/ gets the ``accuracy`` marker; everything
    under tests/accuracy/result_validation/ additionally gets ``result_eval``.
    ``items`` is the whole session list, so we filter by path.
    """
    for item in items:
        try:
            item_path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        try:
            item_path.relative_to(_ACCURACY_DIR)
        except ValueError:
            continue
        item.add_marker(pytest.mark.accuracy)
        if _RESULT_VALIDATION_DIR in item_path.parents:
            item.add_marker(pytest.mark.result_eval)


def _openai_api_key() -> str | None:
    return os.getenv("CB_ACCURACY_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")


def _require_openai() -> str:
    key = _openai_api_key()
    if not key:
        pytest.skip(
            "Accuracy tests require OPENAI_API_KEY (or CB_ACCURACY_OPENAI_API_KEY)."
        )
    return key


@pytest.fixture(scope="session")
def accuracy_run_id() -> str:
    return os.getenv("CB_ACCURACY_RUN_ID", f"local-{int(time.time())}")


@pytest.fixture(scope="session")
def commit_sha() -> str:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
        )
        return sha.decode().strip()
    except Exception:
        return os.getenv("CB_ACCURACY_COMMIT_SHA", "unknown")


@pytest.fixture(scope="session")
def results_dir() -> Path:
    path = Path(
        os.getenv(
            "CB_ACCURACY_RESULTS_DIR",
            str(Path(__file__).resolve().parent / "results"),
        )
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(scope="session")
def result_storage(results_dir: Path) -> DiskResultStorage:
    return DiskResultStorage(results_dir)


@pytest.fixture(scope="session")
def openai_model() -> str:
    return os.getenv("CB_ACCURACY_OPENAI_MODEL", "gpt-4o")


@pytest.fixture(scope="session")
def judge_model(openai_model: str) -> str:
    """Model used by the LLM-as-judge. Defaults to the agent model.

    Override with CB_ACCURACY_JUDGE_MODEL to judge with a different (often
    stronger) model than the one under test.
    """
    return os.getenv("CB_ACCURACY_JUDGE_MODEL", openai_model)


@pytest.fixture()
def openai_agent(openai_model: str) -> OpenAIAgent:
    api_key = _require_openai()
    base_url = os.getenv("CB_ACCURACY_OPENAI_BASE_URL")
    bucket = require_test_bucket()
    scope = get_test_scope()
    collection = get_test_collection()
    extra_prompt = (
        "When the user does not provide explicit bucket / scope / collection names, "
        f"default to bucket='{bucket}', scope='{scope}', collection='{collection}'."
    )
    return OpenAIAgent(
        model=openai_model,
        api_key=api_key,
        base_url=base_url,
        extra_system_prompt=extra_prompt,
    )


@pytest.fixture()
def judge(judge_model: str) -> LLMJudge:
    api_key = _require_openai()
    base_url = os.getenv("CB_ACCURACY_OPENAI_BASE_URL")
    return LLMJudge(model=judge_model, api_key=api_key, base_url=base_url)


@asynccontextmanager
async def _create_mcp_session() -> AsyncIterator[ClientSession]:
    env = _build_env()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        env=env,
    )
    async with asyncio.timeout(DEFAULT_TIMEOUT):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def create_accuracy_client() -> AsyncIterator[AccuracyTestingClient]:
    """Open an MCP session and wrap it for accuracy testing.

    Returns an async context manager so the test body enters and exits the
    underlying MCP / anyio task group in the same asyncio Task. Using an
    async-generator pytest fixture for this leads to anyio's "cancel scope
    entered in different task" error on teardown.
    """
    _require_openai()
    async with _create_mcp_session() as session:
        yield AccuracyTestingClient(session)


@pytest.fixture()
def accuracy_client():
    """Expose the session-opening context manager to tests.

    Tests should consume it as:

        async with accuracy_client() as client:
            ...
    """
    return create_accuracy_client


@pytest.fixture()
def test_bucket() -> str:
    return require_test_bucket()


@pytest.fixture()
def test_scope() -> str:
    return get_test_scope()


@pytest.fixture()
def test_collection() -> str:
    return get_test_collection()
