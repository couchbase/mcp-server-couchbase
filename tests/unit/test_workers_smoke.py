"""End-to-end smoke test for ``--workers``: a real supervisor, real workers.

Everything else about multi-worker mode is tested with Uvicorn patched out.
This is the one test that proves the pieces fit when they are not: the
console script becomes a Uvicorn supervisor, spawned workers import
``mcp_server:create_app`` and rebuild the server from the handed-over config,
and fresh client connections are served statelessly.

No cluster is needed: ``initialize`` and ``tools/list`` run in lazy mode and
never connect. Each run spawns three Python processes, so it costs a few
seconds.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

WORKERS = 2
STARTUP_TIMEOUT_S = 60
CONNECTIONS = 6


def _server_command() -> list[str]:
    script = shutil.which("couchbase-mcp-server", path=str(Path(sys.executable).parent))
    if script is None:
        pytest.skip("couchbase-mcp-server console script is not installed")
    return [script]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early with code {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"server did not listen on {port} in {STARTUP_TIMEOUT_S}s")


async def _list_tools_once(url: str) -> list[str]:
    async with (
        streamable_http_client(url) as (read, write, _session_id),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.list_tools()
        return [tool.name for tool in result.tools]


@pytest.fixture
def workers_server(tmp_path):
    port = _free_port()
    log_file = tmp_path / "mcp_server.log"
    env = {
        **os.environ,
        # Keep the smoke test from emitting startup telemetry.
        "PACKAGE_TRACKER_ANALYTICS": "false",
    }
    for var in ("CB_CONNECTION_STRING", "CB_USERNAME", "CB_PASSWORD"):
        env.pop(var, None)
    proc = subprocess.Popen(
        [
            *_server_command(),
            "--transport",
            "http",
            "--workers",
            str(WORKERS),
            "--port",
            str(port),
            "--log-sinks",
            "file",
            "--log-file",
            str(log_file),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_port(port, proc)
        yield f"http://127.0.0.1:{port}/mcp", tmp_path
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def test_workers_serve_fresh_connections_and_log_per_process(workers_server):
    url, log_dir = workers_server

    async def run() -> list[list[str]]:
        return [await _list_tools_once(url) for _ in range(CONNECTIONS)]

    tool_lists = asyncio.run(run())

    assert tool_lists[0], "no tools registered"
    assert all(names == tool_lists[0] for names in tool_lists), (
        "workers registered different tool sets"
    )

    # Each worker writes its own files, named for this host and its pid; the
    # supervisor keeps the base name. Give slow workers a moment to log.
    pattern = re.compile(r"^mcp_server\.[A-Za-z0-9_-]+\.(\d+)\.info\.log$")
    deadline = time.monotonic() + 10
    pids: set[str] = set()
    while time.monotonic() < deadline:
        pids = {
            m.group(1) for path in log_dir.iterdir() if (m := pattern.match(path.name))
        }
        if len(pids) >= WORKERS:
            break
        time.sleep(0.2)
    assert len(pids) == WORKERS, sorted(p.name for p in log_dir.iterdir())
