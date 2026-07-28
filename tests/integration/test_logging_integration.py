"""End-to-end integration tests for the logging pipeline.

These tests spawn the real MCP server as a subprocess (via the shared
``create_logging_test_session`` helper) and observe the resulting filesystem
state and stderr stream to verify the wiring between Click, ``configure_logging``,
and the on-disk handlers. Unit tests in ``tests/unit/`` cover the individual
functions in isolation; these tests verify they're plumbed correctly through
the server entrypoint.

Cluster credentials are deliberately *not* required: the server boots fine in
lazy mode and the tools needed here (``get_server_configuration_status``) don't
touch the cluster. This keeps the logging integration tests runnable in stock
CI without secrets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import create_logging_test_session, extract_payload

from cb_mcp.utils.constants import BYTES_PER_MB


@pytest.mark.asyncio
async def test_default_file_sinks_create_per_level_files(tmp_path) -> None:
    """``--log-sinks file`` with no path creates per-level files in CWD.

    At the default INFO level the active levels are INFO/WARNING/ERROR, each in
    its own ``mcp_server.<level>.log`` derived from the default ``--log-file``
    base (``mcp_server.log`` -> ``mcp_server.error.log`` etc.).
    """
    async with create_logging_test_session(
        extra_args=["--log-sinks", "file"],
        cwd=tmp_path,
    ):
        pass

    info_file = tmp_path / "mcp_server.info.log"
    err_file = tmp_path / "mcp_server.error.log"
    assert info_file.exists(), "default INFO log not created in CWD"
    assert err_file.exists(), "default ERROR log not created in CWD"
    # The startup summary is an INFO record, so it lands in the INFO file.
    assert "Logging configured" in info_file.read_text()
    # No combined/base file is written under the per-level model.
    assert not (tmp_path / "mcp_server.log").exists()


@pytest.mark.asyncio
async def test_custom_log_file_paths_honoured(tmp_path) -> None:
    """An explicit ``--log-file`` base is honoured; all per-level files derive
    from it (including the ERROR file)."""
    base_path = tmp_path / "subdir-main.log"
    async with create_logging_test_session(
        extra_args=[
            "--log-sinks",
            "file",
            "--log-file",
            str(base_path),
        ],
    ):
        pass

    # All level files derive from the base path by inserting the level name.
    assert (tmp_path / "subdir-main.info.log").exists()
    assert (tmp_path / "subdir-main.error.log").exists()
    # The base path itself is never written, nor the default-named files.
    assert not base_path.exists()
    assert not (tmp_path / "mcp_server.info.log").exists()


@pytest.mark.asyncio
async def test_multiple_sinks_write_to_both_stderr_and_file(tmp_path) -> None:
    """``--log-sinks stderr,file`` writes the same record to BOTH destinations.

    PRD Req 5: "Users can specify multiple values in this option ... the MCP
    server will write the Logs to all of those options." Unit tests verify both
    handlers attach; this verifies a real record actually reaches stderr *and*
    the per-level file through the spawned server.
    """
    base_path = tmp_path / "main.log"
    stderr_path = tmp_path / "server.stderr"
    with stderr_path.open("w", encoding="utf-8") as stderr_file:
        async with create_logging_test_session(
            extra_args=[
                "--log-sinks",
                "stderr,file",
                "--log-file",
                str(base_path),
            ],
            stderr_buffer=stderr_file,
        ):
            pass

    # The startup summary is a single INFO record. With both sinks active it
    # must land in the INFO file AND on stderr.
    info_text = (tmp_path / "main.info.log").read_text()
    stderr_text = stderr_path.read_text()
    assert "Logging configured" in info_text, (
        "INFO record missing from the file sink under --log-sinks stderr,file"
    )
    assert "Logging configured" in stderr_text, (
        "INFO record missing from the stderr sink under --log-sinks stderr,file"
    )


@pytest.mark.asyncio
async def test_default_sink_is_stderr_and_creates_no_files(tmp_path) -> None:
    """With no ``--log-sinks``, output goes to stderr and no log files are written.

    PRD Req 5: "MCP server will write to stderr as a default and when this
    configuration is not set." Guards against a regression where file logging
    silently becomes a default and starts writing files no one asked for.
    """
    stderr_path = tmp_path / "server.stderr"
    with stderr_path.open("w", encoding="utf-8") as stderr_file:
        async with create_logging_test_session(
            cwd=tmp_path,
            stderr_buffer=stderr_file,
        ):
            pass

    stderr_text = stderr_path.read_text()
    assert "Logging configured" in stderr_text, (
        "default stderr sink produced no records on stderr"
    )
    # No per-level files should be created anywhere in the CWD by default.
    created_logs = list(tmp_path.glob("*.log"))
    assert created_logs == [], (
        f"default run (no --log-sinks) unexpectedly created log files: {created_logs}"
    )


@pytest.mark.asyncio
async def test_debug_level_writes_env_info_record(tmp_path) -> None:
    """At DEBUG, ``log_environment_info`` writes a JSON record to the DEBUG file."""
    base_path = tmp_path / "main.log"
    async with create_logging_test_session(
        extra_args=[
            "--log-level",
            "DEBUG",
            "--log-sinks",
            "file",
            "--log-file",
            str(base_path),
        ],
    ):
        pass

    # The env-info record is emitted at DEBUG, so it lands in the DEBUG file.
    content = (tmp_path / "main.debug.log").read_text()
    # The diagnostic record starts with "Environment | " followed by a JSON object.
    assert "Environment | " in content, "env-info DEBUG record missing from log file"
    # Extract and parse the JSON payload to confirm it's well-formed.
    line = next(line for line in content.splitlines() if "Environment | " in line)
    payload = line.split("Environment | ", 1)[1]
    parsed = json.loads(payload)
    # Verify a couple of fields we know about so this isn't just a "is it JSON?" check.
    assert "os" in parsed and "python" in parsed and "logging" in parsed
    assert parsed["logging"]["level"] == "DEBUG"


@pytest.mark.asyncio
async def test_env_var_log_level_equivalent_to_flag(tmp_path) -> None:
    """``CB_MCP_LOG_LEVEL`` env var has the same effect as ``--log-level``."""
    base_path = tmp_path / "main.log"
    async with create_logging_test_session(
        extra_args=[
            "--log-sinks",
            "file",
            "--log-file",
            str(base_path),
        ],
        env_overrides={"CB_MCP_LOG_LEVEL": "DEBUG"},
    ):
        pass

    # Same env-info DEBUG record (in the DEBUG file) confirms the env-var path
    # resolved to DEBUG.
    content = (tmp_path / "main.debug.log").read_text()
    assert "Environment | " in content


@pytest.mark.asyncio
async def test_off_level_silences_couchbase_records_on_stderr(tmp_path) -> None:
    """``--log-level OFF`` produces zero ``couchbase`` records on stderr.

    The MCP SDK's ``stdio_client`` passes ``errlog`` straight through to the
    ``asyncio.subprocess`` machinery, which requires a real file descriptor
    (``fileno()``). An ``io.StringIO`` doesn't qualify; a real file does.
    """
    stderr_path = tmp_path / "server.stderr"
    with stderr_path.open("w", encoding="utf-8") as stderr_file:
        async with create_logging_test_session(
            extra_args=["--log-level", "OFF"],
            stderr_buffer=stderr_file,
        ):
            pass

    stderr = stderr_path.read_text()
    # External loggers (FastMCP, uvicorn) are not adopted under approach-A
    # logging, so we only assert about the ``couchbase`` logger tree.
    assert " - couchbase " not in stderr, (
        f"OFF mode leaked couchbase records to stderr:\n{stderr}"
    )


@pytest.mark.asyncio
async def test_append_on_restart_preserves_history(tmp_path) -> None:
    """Two sequential server starts append to the same per-level log file."""
    base_path = tmp_path / "main.log"
    # The startup summary is an INFO record, so assert against the INFO file.
    info_path = tmp_path / "main.info.log"
    args = [
        "--log-sinks",
        "file",
        "--log-file",
        str(base_path),
    ]
    async with create_logging_test_session(extra_args=args):
        pass
    first_size = info_path.stat().st_size
    first_text = info_path.read_text()
    assert first_size > 0

    async with create_logging_test_session(extra_args=args):
        pass
    second_text = info_path.read_text()

    # File grew (history preserved) and the first run's records are still there.
    assert info_path.stat().st_size > first_size, (
        "log file did not grow on restart — was it truncated instead of appended?"
    )
    assert first_text in second_text, "first-run records were overwritten on restart"
    # Two distinct "Logging configured" lines now (one per run).
    assert second_text.count("Logging configured") >= 2


@pytest.mark.asyncio
async def test_logging_block_exposed_via_mcp_tool(tmp_path) -> None:
    """``get_server_configuration_status`` returns a populated ``logging`` block.

    End-to-end verification of the AppContext.logging_config contract: the CLI
    entrypoint stashes the resolved snapshot on the lifespan context, the tool
    reads it, and the MCP client sees it in the response payload.
    """
    base_path = tmp_path / "main.log"
    async with create_logging_test_session(
        extra_args=[
            "--log-level",
            "DEBUG",
            "--log-sinks",
            "file",
            "--log-file",
            str(base_path),
        ],
    ) as session:
        response = await session.call_tool(
            "get_server_configuration_status", arguments={}
        )
        payload = extract_payload(response)

    assert isinstance(payload, dict)
    logging_block = payload["logging"]
    assert logging_block["level"] == "DEBUG"
    assert sorted(logging_block["sinks"]) == ["file"]
    # All per-level files (including ERROR) derive from the single --log-file base.
    log_files = logging_block["log_files"]
    assert log_files["INFO"] == str(tmp_path / "main.info.log")
    assert log_files["DEBUG"] == str(tmp_path / "main.debug.log")
    assert log_files["ERROR"] == str(tmp_path / "main.error.log")
    # max_bytes and backup_counts are per-level maps keyed by the active levels
    # (size reported in bytes; default 1 MB = 1048576).
    assert logging_block["max_bytes"] == {
        "DEBUG": BYTES_PER_MB,
        "INFO": BYTES_PER_MB,
        "WARNING": BYTES_PER_MB,
        "ERROR": BYTES_PER_MB,
    }
    assert logging_block["backup_counts"] == {
        "DEBUG": 1,
        "INFO": 1,
        "WARNING": 1,
        "ERROR": 1,
    }
    # The dedicated env file is derived from the same --log-file base.
    assert logging_block["server_config_file"] == str(tmp_path / "main_config.log.json")
    # The serialised keys are the plural, per-level forms.
    assert "backup_count" not in logging_block


def test_empty_log_file_rejected_at_startup() -> None:
    """``--log-file ""`` is rejected by Click; server exits non-zero with a clear error.

    Uses ``subprocess.run`` directly because the server fails to start, so the
    MCP-session helper would just see "connection failed" without a useful
    diagnostic. We need to read the exit code and stderr to confirm the
    rejection happened cleanly at the Click validator boundary.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "mcp_server", "--log-file", ""],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=env,
    )
    assert result.returncode != 0, (
        f"server should have rejected empty --log-file, but exited 0\n"
        f"stderr: {result.stderr}"
    )
    assert "path cannot be empty" in result.stderr, (
        f"expected Click rejection message in stderr, got:\n{result.stderr}"
    )


def test_help_renders_without_crashing() -> None:
    """``--help`` exits cleanly and shows the expected options + defaults.

    Catches a class of refactor regressions where a renamed constant
    (e.g. ``default=DEFAULT_LOG_LEVL``) would slip past lint/unit tests but
    crash any user who runs ``--help``. Also asserts ``show_default=True`` is
    still wired — the bracket format would disappear if it ever regressed.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "mcp_server", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=env,
    )
    assert result.returncode == 0, f"--help crashed:\n{result.stderr}"
    # Key options are documented.
    for option in ("--log-level", "--log-sinks", "--log-file"):
        assert option in result.stdout, f"{option} missing from --help"
    # ``show_default=True`` produces a ``[default: ...]`` bracket per option.
    assert "[default:" in result.stdout, (
        "show_default brackets missing — has show_default=True been removed?"
    )


@pytest.mark.asyncio
async def test_log_file_rotates_when_max_bytes_exceeded(tmp_path) -> None:
    """``RotatingFileHandler`` actually rotates once cumulative writes exceed
    ``--log-max-bytes``.

    Drives a deterministic amount of log volume by calling a chatty tool many
    times under ``--log-level DEBUG`` (each call triggers SDK and MCP records).
    The byte budget is set deliberately small (1 KiB) so a handful of calls
    is enough to trigger rotation of the per-level DEBUG file, but the loop
    count is generous so the test isn't sensitive to small record-size shifts.
    """
    base_path = tmp_path / "main.log"
    async with create_logging_test_session(
        extra_args=[
            "--log-level",
            "DEBUG",
            "--log-sinks",
            "file",
            "--log-file",
            str(base_path),
            "--log-max-bytes",
            "1024",
        ],
    ) as session:
        # Each tool call generates ~tens of bytes of DEBUG records; 40 iterations
        # is generous given the 1 KiB cap on the per-level DEBUG file. Backup
        # count defaults to 1 here (configurable via
        # CB_MCP_LOG_RETENTION_BACKUP_COUNT), so a single .1 rollover is what we
        # expect. Higher/zero counts are covered by the retention tests below.
        for _ in range(40):
            await session.call_tool("get_server_configuration_status", arguments={})

    # DEBUG is the highest-volume level (SDK + env-info + entry logs), so its
    # per-level file is the one expected to roll over first.
    debug_path = tmp_path / "main.debug.log"
    rotated = tmp_path / "main.debug.log.1"
    assert rotated.exists(), (
        f"rotation never triggered after 40 tool calls at 1 KiB/file. "
        f"main.debug.log size: "
        f"{debug_path.stat().st_size if debug_path.exists() else 'missing'}"
    )


@pytest.mark.asyncio
async def test_combined_invalid_inputs_degrade_gracefully(tmp_path) -> None:
    """Multiple invalid values (level + sink) both fall back and report errors.

    Catches the "two lenient paths interfere" regression class — e.g. if a
    future refactor accidentally short-circuited one fallback when the other
    fired. Both should produce their own deferred error log records, and the
    server must still start.
    """
    main_path = tmp_path / "main.log"
    async with create_logging_test_session(
        extra_args=[
            "--log-level",
            "BOGUS_LEVEL",
            # ``file`` is in the sink list so the error file actually gets
            # created and we can grep it for both deferred error records.
            "--log-sinks",
            "stderr,file,foo_sink",
            "--log-file",
            str(main_path),
        ],
    ):
        pass

    # Both deferred error records should appear in the ERROR file (ERROR records
    # land there; it derives from the base path: main.log -> main.error.log).
    err_text = (tmp_path / "main.error.log").read_text()
    assert "BOGUS_LEVEL" in err_text, (
        f"invalid level fallback missing from error log:\n{err_text}"
    )
    assert "foo_sink" in err_text, (
        f"invalid sink fallback missing from error log:\n{err_text}"
    )


async def _restart_server_n_times(extra_args: list[str], times: int) -> None:
    """Boot and cleanly shut down the server ``times`` times with the same args.

    Per-tool-call log volume is effectively zero (the config tool logs nothing),
    so rotation is driven by the one-time startup records instead. Each restart
    appends the ~2 KB DEBUG env-info record to the per-level files; with a small
    ``--log-max-bytes`` that reliably forces rollovers without depending on
    fragile per-call byte estimates.
    """
    for _ in range(times):
        async with create_logging_test_session(extra_args=extra_args):
            pass


@pytest.mark.asyncio
async def test_configurable_backup_count_keeps_multiple_backups(tmp_path) -> None:
    """``--log-retention-backup-count N`` retains up to N rotated backups on disk.

    Req 1 end-to-end: with the count set to 2 and a small byte budget, repeated
    startups must produce ``.1`` and ``.2`` rotations of the DEBUG file but never
    ``.3`` — the count both takes effect (>1 backup) and is capped (no N+1).
    """
    base_path = tmp_path / "main.log"
    await _restart_server_n_times(
        [
            "--log-level",
            "DEBUG",
            "--log-sinks",
            "file",
            "--log-file",
            str(base_path),
            "--log-max-bytes",
            "1000",
            "--log-retention-backup-count",
            "2",
        ],
        times=4,
    )

    assert (tmp_path / "main.debug.log.1").exists(), "first backup (.1) never created"
    assert (tmp_path / "main.debug.log.2").exists(), (
        "second backup (.2) missing — configured count of 2 did not take effect"
    )
    # The count is capped at 2: a third backup must never exist.
    assert not (tmp_path / "main.debug.log.3").exists(), (
        "backup count cap breached — .3 exists despite count=2"
    )


@pytest.mark.asyncio
async def test_zero_backup_count_keeps_only_live_file(tmp_path) -> None:
    """``--log-retention-backup-count 0`` keeps only the live file, still bounded.

    Req 1 behaviour: 0 means no rotated backups. The live file is truncated on
    rollover (via the bounded handler) instead of growing unbounded, so across
    repeated startups no ``.N`` files appear and the live file stays bounded
    rather than accumulating every run's volume.
    """
    base_path = tmp_path / "main.log"
    await _restart_server_n_times(
        [
            "--log-level",
            "DEBUG",
            "--log-sinks",
            "file",
            "--log-file",
            str(base_path),
            "--log-max-bytes",
            "1000",
            "--log-retention-backup-count",
            "0",
        ],
        times=4,
    )

    debug_path = tmp_path / "main.debug.log"
    assert debug_path.exists(), "live DEBUG file missing"
    # No rotated backups of any level should exist at count=0. Rotated files are
    # numbered (main.<level>.log.1, .2, ...); the *.log.[0-9]* glob excludes the
    # dedicated main_config.log.json snapshot.
    rotated = list(tmp_path.glob("*.log.[0-9]*"))
    assert not rotated, f"count=0 created rotated backups: {rotated}"
    # The live file stayed bounded: 4 restarts append ~2 KB each (~8 KB) without
    # rotation; truncation on rollover keeps it well under that.
    assert debug_path.stat().st_size <= 4096, (
        f"live file grew unbounded at count=0: {debug_path.stat().st_size} bytes"
    )


@pytest.mark.asyncio
async def test_per_level_retention_and_size_via_env_reflected_in_snapshot(
    tmp_path,
) -> None:
    """Per-level size + retention env vars flow through to the MCP tool snapshot.

    Reqs 1 & 2 end-to-end via the ``CB_MCP_LOG_*`` env vars (the config surface
    operators actually use): sizes are configured in MB via the canonical
    ``CB_MCP_LOG_ROTATION_MAX_SIZE_MB`` and per-level ``*_ROTATION_MAX_SIZE_MB``, unset
    levels inherit the global, explicit per-level values win, and
    ``get_server_configuration_status`` reports the resolved per-level maps
    (size in bytes).
    """
    base_path = tmp_path / "main.log"
    async with create_logging_test_session(
        extra_args=[
            "--log-level",
            "DEBUG",
            "--log-sinks",
            "file",
            "--log-file",
            str(base_path),
        ],
        env_overrides={
            "CB_MCP_LOG_ROTATION_MAX_SIZE_MB": "1",  # 1 MB global (canonical)
            "CB_MCP_LOG_INFO_ROTATION_MAX_SIZE_MB": "2",  # 2 MB INFO override
            "CB_MCP_LOG_RETENTION_BACKUP_COUNT": "2",
            "CB_MCP_LOG_ERROR_RETENTION_BACKUP_COUNT": "5",
        },
    ) as session:
        response = await session.call_tool(
            "get_server_configuration_status", arguments={}
        )
        payload = extract_payload(response)

    logging_block = payload["logging"]
    # Size (bytes): INFO overridden to 2 MB, every other level inherits 1 MB.
    assert logging_block["max_bytes"] == {
        "DEBUG": BYTES_PER_MB,
        "INFO": 2 * BYTES_PER_MB,
        "WARNING": BYTES_PER_MB,
        "ERROR": BYTES_PER_MB,
    }
    # Retention: ERROR overridden to 5, every other level inherits the 2 global.
    assert logging_block["backup_counts"] == {
        "DEBUG": 2,
        "INFO": 2,
        "WARNING": 2,
        "ERROR": 5,
    }


@pytest.mark.asyncio
async def test_zero_rotation_size_falls_back_to_default_with_warning(tmp_path) -> None:
    """A rotation size of 0 is rejected at startup: warn + fall back to default.

    0 is invalid (it never means "disable rotation"). The server must still
    start, log a warning naming the offending variable, and resolve the size to
    the package default (verified via the tool snapshot). Uses the canonical
    ``--log-rotation-max-size-mb`` (MB).
    """
    base_path = tmp_path / "main.log"
    stderr_path = tmp_path / "server.stderr"
    with stderr_path.open("w", encoding="utf-8") as stderr_file:
        async with create_logging_test_session(
            extra_args=[
                "--log-level",
                "DEBUG",
                "--log-sinks",
                "stderr,file",
                "--log-file",
                str(base_path),
                "--log-rotation-max-size-mb",
                "0",
            ],
            stderr_buffer=stderr_file,
        ) as session:
            response = await session.call_tool(
                "get_server_configuration_status", arguments={}
            )
            payload = extract_payload(response)

    # The warning names the offending variable and is visible on stderr.
    stderr_text = stderr_path.read_text()
    assert "CB_MCP_LOG_ROTATION_MAX_SIZE_MB=0" in stderr_text, (
        f"missing 0-is-invalid warning on stderr:\n{stderr_text}"
    )
    # Every level resolved to the 1 MB package default (1048576 bytes) rather than 0.
    assert payload["logging"]["max_bytes"] == {
        "DEBUG": BYTES_PER_MB,
        "INFO": BYTES_PER_MB,
        "WARNING": BYTES_PER_MB,
        "ERROR": BYTES_PER_MB,
    }


@pytest.mark.asyncio
async def test_deprecated_max_bytes_warns_but_is_honored(tmp_path) -> None:
    """The deprecated ``--log-max-bytes`` (bytes) still works, with a warning.

    Backward compatibility: existing configs using CB_MCP_LOG_MAX_BYTES keep
    working (in bytes), but a deprecation warning steers operators to the
    canonical ``--log-rotation-max-size-mb`` (MB).
    """
    base_path = tmp_path / "main.log"
    stderr_path = tmp_path / "server.stderr"
    with stderr_path.open("w", encoding="utf-8") as stderr_file:
        async with create_logging_test_session(
            extra_args=[
                "--log-level",
                "DEBUG",
                "--log-sinks",
                "stderr,file",
                "--log-file",
                str(base_path),
                "--log-max-bytes",
                "4096",
            ],
            stderr_buffer=stderr_file,
        ) as session:
            response = await session.call_tool(
                "get_server_configuration_status", arguments={}
            )
            payload = extract_payload(response)

    stderr_text = stderr_path.read_text()
    assert "CB_MCP_LOG_MAX_BYTES is deprecated" in stderr_text, (
        f"missing deprecation warning on stderr:\n{stderr_text}"
    )
    # Honored verbatim (4096 bytes) across every level.
    assert payload["logging"]["max_bytes"] == {
        "DEBUG": 4096,
        "INFO": 4096,
        "WARNING": 4096,
        "ERROR": 4096,
    }


@pytest.mark.asyncio
async def test_fractional_rotation_size_reflected_in_snapshot(tmp_path) -> None:
    """A fractional MB rotation size (0.5) is accepted and stored as exact bytes."""
    base_path = tmp_path / "main.log"
    async with create_logging_test_session(
        extra_args=[
            "--log-level",
            "DEBUG",
            "--log-sinks",
            "file",
            "--log-file",
            str(base_path),
        ],
        env_overrides={"CB_MCP_LOG_ROTATION_MAX_SIZE_MB": "0.5"},
    ) as session:
        response = await session.call_tool(
            "get_server_configuration_status", arguments={}
        )
        payload = extract_payload(response)

    # 0.5 MB -> exactly 524288 bytes, no rounding.
    assert payload["logging"]["max_bytes"] == {
        "DEBUG": BYTES_PER_MB // 2,
        "INFO": BYTES_PER_MB // 2,
        "WARNING": BYTES_PER_MB // 2,
        "ERROR": BYTES_PER_MB // 2,
    }


@pytest.mark.asyncio
async def test_env_info_captured_in_dedicated_file_at_info(tmp_path) -> None:
    """Req 3: the environment snapshot lands in its own file, even at INFO.

    At the default INFO level no DEBUG file is created, yet the dedicated
    ``_config.log.json`` file must still capture the snapshot — proving it is
    level-independent and lives outside the rotating per-level files (so a
    debug-file rotation can't lose it).
    """
    base_path = tmp_path / "main.log"
    async with create_logging_test_session(
        extra_args=[
            "--log-sinks",
            "file",
            "--log-file",
            str(base_path),
        ],
    ):
        pass

    server_config_file = tmp_path / "main_config.log.json"
    assert server_config_file.exists(), "dedicated config file not created at INFO"
    # No DEBUG file at INFO — the record only survives because it has its own
    # file, not because it rode along in the (absent) debug log.
    assert not (tmp_path / "main.debug.log").exists()

    # The file is pure JSON (a single object).
    parsed = json.loads(server_config_file.read_text())
    assert "os" in parsed and "python" in parsed and "logging" in parsed


@pytest.mark.asyncio
async def test_server_config_file_survives_across_restarts_as_current_snapshot(
    tmp_path,
) -> None:
    """The config file is overwritten each start, so it always holds one current
    record and never grows across restarts."""
    base_path = tmp_path / "main.log"
    args = ["--log-sinks", "file", "--log-file", str(base_path)]
    await _restart_server_n_times(args, times=3)

    server_config_file = tmp_path / "main_config.log.json"
    assert server_config_file.exists()
    # Overwrite (not append): still a single valid JSON object after three starts.
    assert isinstance(json.loads(server_config_file.read_text()), dict)


@pytest.mark.asyncio
async def test_config_file_survives_debug_rotation(tmp_path) -> None:
    """the config snapshot is independent of per-level rotation.

    Even after the DEBUG file rotates (forced with a tiny byte cap so the ~2 KB
    startup env record rolls it over), the dedicated JSON snapshot is intact and
    holds current, valid JSON.
    """
    base_path = tmp_path / "main.log"
    # Small byte cap via the deprecated bytes var so the DEBUG env record forces
    # a rollover; the MB var's 1 MB floor wouldn't rotate on ~2 KB.
    args = [
        "--log-level",
        "DEBUG",
        "--log-sinks",
        "file",
        "--log-file",
        str(base_path),
        "--log-max-bytes",
        "1024",
    ]
    await _restart_server_n_times(args, times=3)

    # The DEBUG file rotated (default retention keeps one backup) ...
    assert (tmp_path / "main.debug.log.1").exists(), "DEBUG file did not rotate"
    # ... but the dedicated config file survives and holds current, valid JSON.
    config_file = tmp_path / "main_config.log.json"
    assert config_file.exists()
    parsed = json.loads(config_file.read_text())
    assert "os" in parsed and "logging" in parsed
