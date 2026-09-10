"""Loading and searching the reference datasets bundled under ``cb_mcp/reference_data``.

Each dataset is a JSON Lines file: line 1 is the envelope (which tool it serves, which fields
are searchable, which are browsable "chapters"), every subsequent line is one record. See
``cb_mcp/reference_data/README.md`` for the format contract.

Everything here streams. A dataset is never held in memory as a whole -- searching keeps only a
top-K heap, and browsing keeps only per-chapter counters (capped at 25 values by the format).
That keeps memory flat regardless of dataset size, which is why the format is JSONL rather than
a JSON array: ``json.loads`` on a single ``[...]`` has to materialise every record before
yielding the first one, and the standard library has no streaming JSON parser.
"""

import heapq
import json
import logging
import os
import re
from collections import Counter
from functools import cache, lru_cache
from importlib.resources import files
from typing import Any

from .constants import MCP_SERVER_NAME

logger = logging.getLogger(f"{MCP_SERVER_NAME}.utils.reference_data")

REFERENCE_DATA_PACKAGE = "cb_mcp.reference_data"
DATASET_SUFFIX = ".jsonl"
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

# Format caps, enforced by tests/unit/test_reference_data_conformance.py. A dataset's chapter
# listing is inlined in every tool response, so it has to stay small enough to be free.
MAX_CHAPTER_FIELDS = 3
MAX_CHAPTER_VALUES = 25


def _dataset_dir() -> str:
    """Locate the bundled reference_data directory.

    Mirrors ``_get_capella_root_ca_path`` in index_utils: importlib.resources for installed
    packages, with a path-based fallback for running straight from a source checkout.
    """
    try:
        return str(files(REFERENCE_DATA_PACKAGE))
    except (ImportError, FileNotFoundError, TypeError, NotADirectoryError):
        utils_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(os.path.dirname(utils_dir), "reference_data")


def _dataset_paths() -> list[str]:
    directory = _dataset_dir()
    if not os.path.isdir(directory):
        logger.warning(f"Reference data directory not found: {directory}")
        return []
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(DATASET_SUFFIX)
    )


def normalize_key(value: str) -> str:
    """Fold a tool name to a lookup key, so "Get Cluster Metrics" == "get-cluster-metrics"."""
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


@cache
def load_envelope(path: str) -> dict[str, Any]:
    """Read line 1 of a dataset. Cheap enough that registry building reads every file."""
    with open(path, encoding="utf-8") as handle:
        first_line = handle.readline()
    if not first_line.strip():
        raise ValueError(
            f"{os.path.basename(path)} is empty; line 1 must be the envelope"
        )
    envelope = json.loads(first_line)
    if not isinstance(envelope, dict):
        raise ValueError(f"{os.path.basename(path)} line 1 must be a JSON object")
    return envelope


@lru_cache(maxsize=1)
def _registry() -> dict[str, str]:
    """Map every normalized tool name and dataset_id to the dataset path serving it."""
    registry: dict[str, str] = {}
    for path in _dataset_paths():
        try:
            envelope = load_envelope(path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            # One malformed dataset must not take the whole tool offline.
            logger.error(f"Skipping unreadable reference dataset {path}: {e}")
            continue
        keys = [envelope.get("dataset_id", ""), *envelope.get("tools", [])]
        for key in keys:
            if key:
                registry[normalize_key(key)] = path
    return registry


def registered_tool_names() -> list[str]:
    """Every name accepted as `tool_name`, for self-correcting error messages."""
    names: set[str] = set()
    for path in _dataset_paths():
        try:
            envelope = load_envelope(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        names.update(envelope.get("tools", []))
    return sorted(names)


def resolve_dataset(tool_name: str) -> str | None:
    """Return the dataset path serving `tool_name`, or None if nothing is registered for it."""
    return _registry().get(normalize_key(tool_name or ""))


def iter_records(path: str):
    """Yield records one at a time, skipping the envelope. Never materialises the file."""
    with open(path, encoding="utf-8") as handle:
        handle.readline()  # envelope
        for line_number, line in enumerate(handle, start=2):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                logger.error(
                    f"{os.path.basename(path)} line {line_number} is not valid JSON: {e}"
                )


@cache
def chapters(path: str) -> dict[str, dict[str, int]]:
    """Value counts per chapter field. Empty dict when the dataset declares no chapters.

    The result is bounded by MAX_CHAPTER_VALUES, so caching it is bounded too -- unlike caching
    the records themselves, which is what this module deliberately avoids.
    """
    chapter_fields = load_envelope(path).get("chapter_fields") or []
    if not chapter_fields:
        return {}

    counters: dict[str, Counter] = {field: Counter() for field in chapter_fields}
    for record in iter_records(path):
        for field in chapter_fields:
            value = record.get(field)
            if value is not None:
                counters[field][value] += 1
    return {field: dict(counter.most_common()) for field, counter in counters.items()}


def sample_record(path: str) -> dict[str, Any] | None:
    """The dataset's first record, to show callers the shape of what they're searching."""
    for record in iter_records(path):
        return record
    return None


def _searchable_text(record: dict[str, Any], field_spec: dict[str, Any]) -> str:
    value = record.get(field_spec["field"])
    if value is None:
        return ""
    text = str(value)
    # Identifiers like kv_ep_diskqueue_fill are one token to a token-based scorer; splitting on
    # underscores lets keywords such as ["disk", "queue"] match their parts.
    if field_spec.get("split_underscores"):
        text = text.replace("_", " ")
    return text


def search(
    path: str,
    query: str,
    chapter_filters: dict[str, str] | None = None,
    max_results: int = 25,
    min_score: float = 55.0,
) -> tuple[list[dict[str, Any]], int]:
    """Stream the dataset and return (top matches, total number of matches).

    Imported lazily: rapidfuzz is a compiled extension costing ~3.6 MB resident, and a server
    that never calls this tool should not pay for it.
    """
    from rapidfuzz import fuzz  # noqa: PLC0415 — deliberately lazy, see docstring

    envelope = load_envelope(path)
    search_fields = envelope.get("search_fields") or []
    id_field = envelope.get("id_field", "")
    filters = chapter_filters or {}

    # (score, tiebreak_len, tiebreak_id, record) -- heapq keeps the smallest, so the heap holds
    # the best `max_results` seen so far and everything else is discarded as we go.
    heap: list[tuple[float, int, str, dict[str, Any]]] = []
    total_matches = 0

    for record in iter_records(path):
        if any(record.get(field) != value for field, value in filters.items()):
            continue

        score = 0.0
        for field_spec in search_fields:
            text = _searchable_text(record, field_spec)
            if not text:
                continue
            weight = float(field_spec.get("weight", 1.0))
            score = max(score, weight * fuzz.token_set_ratio(query, text))

        if score < min_score:
            continue

        total_matches += 1
        id_value = str(record.get(id_field, ""))
        # Negated length so that, at equal scores, heapq evicts the LONGER id first --
        # "sys_disk_queue" should outrank "sys_disk_queue_depth".
        entry = (round(score, 2), -len(id_value), id_value, record)
        if len(heap) < max_results:
            heapq.heappush(heap, entry)
        elif max_results > 0 and entry > heap[0]:
            heapq.heapreplace(heap, entry)

    ranked = sorted(heap, key=lambda item: (-item[0], -item[1], item[2]))
    results = [{**record, "score": score} for score, _, _, record in ranked]
    return results, total_matches
