"""Configuration for large-result handling.

Kept in its own module, free of EA and FastMCP imports, so the truncation
logic in ``result_handling`` can be unit-tested against a hand-built config
with no cluster, no server, and no disk.

The three knobs map 1:1 to server CLI options / env vars; see
``ea_mcp_server.main``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BYTES_PER_MB = 1024 * 1024

#: Results at or below this size are returned in full. Above it they are
#: truncated, and (when saving is enabled and the caller opted in) the full
#: result is kept for retrieval as an MCP resource.
DEFAULT_TRUNCATE_BYTES = 1 * BYTES_PER_MB  # 1 MB

#: Total on-disk budget for saved results. Past this, the least-recently-used
#: saved results are deleted to make room.
DEFAULT_STORAGE_MAX_BYTES = 1024 * BYTES_PER_MB  # 1 GB


@dataclass(frozen=True)
class ResultConfig:
    """Resolved large-result settings for one server process.

    Frozen because these are startup options: nothing should mutate them
    per-request, and freezing makes that a type error rather than a bug.
    """

    #: Master switch (``--save-large-results``). When False, oversized results
    #: are only truncated -- nothing is ever written to disk, regardless of
    #: what an individual tool call asks for.
    save_large_results: bool = False

    #: Size threshold *and* truncation target, in bytes.
    truncate_bytes: int = DEFAULT_TRUNCATE_BYTES

    #: Disk budget for saved results, in bytes. Only saved (sync) results
    #: count against it; async results live on the EA server, not ours.
    storage_max_bytes: int = DEFAULT_STORAGE_MAX_BYTES

    #: Directory holding saved result files. None means "pick a temp dir".
    storage_path: Path | None = None
