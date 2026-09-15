"""Conformance tests for every reference dataset bundled under cb_mcp/reference_data.

These run over *every* `.jsonl` in that directory, so a dataset added later is validated the
moment it lands -- no test edit required. Failures name the file, the field, and the offending
values, so a dataset author can fix the data without reading this file.

The format contract they enforce is documented in cb_mcp/reference_data/README.md.
"""

import json
import os
from collections import Counter

import pytest

from cb_mcp.utils.reference_data import (
    MAX_CHAPTER_FIELDS,
    MAX_CHAPTER_VALUES,
    SUPPORTED_SCHEMA_VERSIONS,
    _dataset_paths,
    chapters,
    iter_records,
    load_envelope,
)

REQUIRED_ENVELOPE_FIELDS = {
    "schema_version": int,
    "dataset_id": str,
    "title": str,
    "tools": list,
    "source_url": str,
    "generated_at": str,
    "record_count": int,
    "id_field": str,
    "search_fields": list,
}

DATASET_PATHS = _dataset_paths()
DATASET_IDS = [os.path.basename(p) for p in DATASET_PATHS]


def test_at_least_one_dataset_is_bundled():
    """Guards against the data silently not being packaged at all."""
    assert DATASET_PATHS, (
        "No .jsonl datasets found in cb_mcp/reference_data. If this fails after a packaging "
        "change, the dataset files are probably not being included in the distribution."
    )


@pytest.mark.parametrize("path", DATASET_PATHS, ids=DATASET_IDS)
class TestDatasetConformance:
    """Every rule in reference_data/README.md, checked per dataset file."""

    def test_envelope_has_required_fields_with_correct_types(self, path):
        envelope = load_envelope(path)
        name = os.path.basename(path)

        for field, expected_type in REQUIRED_ENVELOPE_FIELDS.items():
            assert field in envelope, (
                f"{name}: envelope is missing required field {field!r}"
            )
            assert isinstance(envelope[field], expected_type), (
                f"{name}: envelope field {field!r} should be "
                f"{expected_type.__name__}, got {type(envelope[field]).__name__}"
            )

        assert envelope["schema_version"] in SUPPORTED_SCHEMA_VERSIONS, (
            f"{name}: schema_version {envelope['schema_version']} is not supported "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
        assert envelope["tools"], f"{name}: 'tools' must name at least one tool"

    def test_every_record_line_is_a_json_object(self, path):
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as handle:
            handle.readline()  # envelope
            for line_number, line in enumerate(handle, start=2):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    pytest.fail(f"{name} line {line_number}: not valid JSON ({e})")
                assert isinstance(record, dict), (
                    f"{name} line {line_number}: each record line must be a JSON object, "
                    f"got {type(record).__name__}"
                )

    def test_record_count_matches_actual_records(self, path):
        envelope = load_envelope(path)
        actual = sum(1 for _ in iter_records(path))
        assert envelope["record_count"] == actual, (
            f"{os.path.basename(path)}: envelope says record_count="
            f"{envelope['record_count']} but the file has {actual} records. "
            "Regenerate the dataset."
        )

    def test_id_field_is_present_and_unique(self, path):
        envelope = load_envelope(path)
        id_field = envelope["id_field"]
        name = os.path.basename(path)

        seen = Counter()
        for index, record in enumerate(iter_records(path), start=2):
            assert id_field in record, (
                f"{name} line {index}: record is missing its id_field {id_field!r}"
            )
            seen[record[id_field]] += 1

        duplicates = [value for value, count in seen.items() if count > 1]
        assert not duplicates, (
            f"{name}: id_field {id_field!r} must be unique, but these values repeat: "
            f"{sorted(duplicates)[:10]}"
        )

    def test_every_search_field_exists_on_every_record(self, path):
        envelope = load_envelope(path)
        name = os.path.basename(path)
        search_fields = envelope["search_fields"]
        assert search_fields, f"{name}: 'search_fields' must not be empty"

        for spec in search_fields:
            assert isinstance(spec, dict) and "field" in spec, (
                f"{name}: each search_fields entry must be an object with a 'field' key, "
                f"got {spec!r}"
            )

        field_names = [spec["field"] for spec in search_fields]
        for index, record in enumerate(iter_records(path), start=2):
            missing = [field for field in field_names if field not in record]
            assert not missing, (
                f"{name} line {index}: record is missing declared search field(s) {missing}"
            )

    def test_chapter_fields_stay_within_the_caps(self, path):
        """Chapters are inlined in every response, so they must stay small.

        Skipped-by-design when a dataset declares no chapters: search-only datasets are valid.
        """
        envelope = load_envelope(path)
        name = os.path.basename(path)
        chapter_fields = envelope.get("chapter_fields") or []
        if not chapter_fields:
            return

        assert len(chapter_fields) <= MAX_CHAPTER_FIELDS, (
            f"{name}: {len(chapter_fields)} chapter fields ({chapter_fields}) exceeds the "
            f"limit of {MAX_CHAPTER_FIELDS}. Drop one, or pick a coarser field."
        )

        dataset_chapters = chapters(path)
        per_field = {field: len(values) for field, values in dataset_chapters.items()}
        total = sum(per_field.values())
        assert total <= MAX_CHAPTER_VALUES, (
            f"{name}: chapters have {total} distinct values in total "
            f"({per_field}), exceeding the limit of {MAX_CHAPTER_VALUES}. "
            "Pick a coarser field -- an ordinal field like a version number is usually the "
            "culprit and should not be a chapter at all."
        )

    def test_chapter_fields_are_never_null(self, path):
        """Null chapter values would need special-casing in every filter comparison."""
        envelope = load_envelope(path)
        name = os.path.basename(path)
        chapter_fields = envelope.get("chapter_fields") or []
        if not chapter_fields:
            return

        for index, record in enumerate(iter_records(path), start=2):
            for field in chapter_fields:
                assert field in record, (
                    f"{name} line {index}: record is missing chapter field {field!r}"
                )
                assert record[field] is not None, (
                    f"{name} line {index}: chapter field {field!r} is null. Give missing "
                    "values an explicit bucket name via the envelope's 'null_labels'."
                )


def test_tool_names_and_dataset_ids_do_not_collide():
    """Two datasets claiming the same tool name would make resolution order-dependent."""
    claimed: dict[str, str] = {}
    for path in DATASET_PATHS:
        envelope = load_envelope(path)
        name = os.path.basename(path)
        for key in [envelope.get("dataset_id", ""), *envelope.get("tools", [])]:
            if not key:
                continue
            assert key not in claimed, (
                f"{name} claims {key!r}, which {claimed[key]} already claims. "
                "Each tool name and dataset_id may be served by only one dataset."
            )
            claimed[key] = name
