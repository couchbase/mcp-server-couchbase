"""Tests for the audit event catalogue and its shipped descriptor.

The event ids are a wire contract: once a customer writes a SIEM rule against
``61522`` it cannot be changed. These tests pin every id from the PRD literally
rather than recomputing them from the same helpers the implementation uses, so
an accidental renumbering fails loudly instead of silently agreeing with itself.

Coverage map:
- literal PRD ids for every core and tool-call event
- block layout arithmetic (read offset, write delta, category order)
- filterability policy: writes never filterable, reads filterable
- descriptor.json matches the in-code catalogue exactly
- events deliberately absent (disabled-tool, session-terminated)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cb_mcp.audit import catalog
from cb_mcp.audit.catalog import (
    CORE_BLOCK,
    OPERATIONAL_BLOCK,
    READ_OFFSET,
    WRITE_DELTA,
    AuditEvent,
    all_tool_call_events,
    build_descriptor,
    categories_for,
    tool_call_event,
    tool_call_event_id,
)

DESCRIPTOR_PATH = Path(catalog.__file__).with_name("descriptor.json")


def test_block_bases_match_the_prd():
    assert CORE_BLOCK == 57344  # 0xE000
    assert OPERATIONAL_BLOCK == 61440  # 0xF000
    assert READ_OFFSET == 48
    assert WRITE_DELTA == 32


def test_category_order_is_the_wire_contract():
    # The list index is the ``n`` in base+48+n, so reordering silently
    # renumbers every tool-call event.
    assert categories_for("operational") == (
        "cluster",
        "schema",
        "kv",
        "query",
        "index",
        "performance",
        "search",
        "config",
    )


@pytest.mark.parametrize(
    ("event", "expected_id", "expected_name"),
    [
        (AuditEvent.SERVER_STARTED, 57344, "server started"),
        (AuditEvent.SERVER_STOPPED, 57345, "server stopped"),
        (AuditEvent.SERVER_CONFIGURATION, 57346, "server configuration"),
        (AuditEvent.SESSION_INITIALIZED, 57360, "session initialized"),
        (AuditEvent.TOKEN_REJECTED, 57376, "token rejected"),
        (AuditEvent.SCOPE_CHECK_DENIED, 57377, "scope check denied"),
        (
            AuditEvent.WRITE_BLOCKED_READ_ONLY,
            57488,
            "write blocked (read-only mode)",
        ),
        (AuditEvent.CONFIRMATION_DECLINED, 57490, "confirmation declined"),
        (AuditEvent.CONFIRMATION_SKIPPED, 57491, "confirmation skipped"),
    ],
)
def test_core_event_ids_are_the_prd_values(event, expected_id, expected_name):
    assert event.id == expected_id
    assert event.event_name == expected_name
    assert event.description


@pytest.mark.parametrize(
    ("category", "read_id", "write_id"),
    [
        ("cluster", 61488, 61520),
        ("schema", 61489, 61521),
        ("kv", 61490, 61522),
        ("query", 61491, 61523),
        ("index", 61492, 61524),
        ("performance", 61493, 61525),
    ],
)
def test_tool_call_ids_are_the_prd_values(category, read_id, write_id):
    assert tool_call_event_id(category, "read", "operational") == read_id
    assert tool_call_event_id(category, "write", "operational") == write_id
    assert write_id - read_id == WRITE_DELTA


def test_kv_events_use_the_document_label():
    # The PRD names the KV events "document read"/"document write" even though
    # the category axis calls the category "kv".
    assert tool_call_event("kv", "read", "operational").event_name == "document read"
    assert tool_call_event("kv", "write", "operational").event_name == "document write"


def test_unknown_category_and_class_are_rejected():
    with pytest.raises(ValueError, match="Unknown audit category"):
        tool_call_event_id("nonsense", "read", "operational")
    with pytest.raises(ValueError, match="must be 'read' or 'write'"):
        tool_call_event_id("kv", "sideways", "operational")


def test_writes_are_never_filterable_and_reads_always_are():
    for event in all_tool_call_events("operational"):
        assert event.filterable is (event.operation_class == "read")


def test_security_and_lifecycle_events_are_not_filterable():
    for event in AuditEvent:
        if event is AuditEvent.SESSION_INITIALIZED:
            assert event.filterable is True
        else:
            assert event.filterable is False, f"{event.event_name} must be mandated"


def test_ids_are_unique():
    ids = [event.id for event in AuditEvent] + [
        event.id for event in all_tool_call_events("operational")
    ]
    assert len(ids) == len(set(ids))


def test_events_named_in_the_prd_but_not_emitted_are_absent():
    """Two ids the PRD names are deliberately not in the catalogue.

    57489 (tool invocation blocked - disabled): withheld tools are never
    registered, so the model is never told they exist and there is no refusal
    to record. 57361 (session terminated): FastMCP exposes no session-end hook,
    so the event cannot be emitted reliably.

    Both must stay absent rather than be defined and never fired — an id in the
    catalogue is an id a SIEM rule can be written against.
    """
    ids = {event.id for event in AuditEvent}
    assert 57489 not in ids
    assert 57361 not in ids


def test_shipped_descriptor_matches_the_code():
    """The descriptor is the operator-facing contract; it must not drift.

    Regenerate with::

        python -c "import json,sys; sys.path.insert(0,'src'); \
from cb_mcp.audit.catalog import build_descriptor; \
print(json.dumps(build_descriptor(), indent=2))" > src/cb_mcp/audit/descriptor.json
    """
    assert DESCRIPTOR_PATH.exists(), "descriptor.json must ship with the package"
    shipped = json.loads(DESCRIPTOR_PATH.read_text(encoding="utf-8"))
    assert shipped == build_descriptor()


def test_descriptor_entries_are_well_formed():
    shipped = json.loads(DESCRIPTOR_PATH.read_text(encoding="utf-8"))
    assert shipped["blocks"] == {
        "core": 57344,
        "operational": 61440,
        "operational_insights": 65536,
    }
    assert shipped["service_packages"] == ["operational", "operational_insights"]
    ids = [entry["id"] for entry in shipped["events"]]
    assert ids == sorted(ids), "descriptor must be ordered by id"
    for entry in shipped["events"]:
        required = {
            "id",
            "name",
            "description",
            "category",
            "default_enabled",
            "filterable",
        }
        assert required <= set(entry), entry
        # Tool-call names repeat across blocks -- "query read" is 61491 in
        # operational and 65587 in operational_insights -- so a Tier-2 entry
        # must name its package or a SIEM author cannot tell them apart. Core
        # events are shared by every package and carry no such field.
        if entry["category"] == "tool_call":
            assert entry["service_package"] in {"operational", "operational_insights"}
            assert set(entry) == required | {"service_package"}
        else:
            assert set(entry) == required
        assert entry["default_enabled"] is True
        assert entry["description"].strip()


# ---------------------------------------------------------------------------
# slot allocation
# ---------------------------------------------------------------------------


def test_category_slots_and_categories_cannot_disagree():
    """``categories_for`` is derived from the slot table, ordered by slot."""
    assert tuple(
        name
        for name, _ in sorted(
            catalog.CATEGORY_SLOTS["operational"].items(), key=lambda kv: kv[1]
        )
    ) == catalog.categories_for("operational")


def test_slot_numbers_are_unique_and_within_the_template():
    slots = list(catalog.CATEGORY_SLOTS["operational"].values())
    assert len(slots) == len(set(slots)), "two categories share a slot"
    assert all(0 <= slot <= catalog.MAX_SLOT for slot in slots)


def test_slot_table_pins_the_shipped_numbers():
    """These numbers are a wire contract. Changing one renumbers a live event.

    Pinned literally so an edit to the table has to be a deliberate, visible
    test change rather than something that quietly renumbers a customer's SIEM
    rule.
    """
    assert catalog.CATEGORY_SLOTS["operational"] == {
        "cluster": 0,
        "schema": 1,
        "kv": 2,
        "query": 3,
        "index": 4,
        "performance": 5,
        # Both reuse retired slots, which is permitted only because neither
        # retired category ever emitted a record. The allocation is contiguous
        # across 0-7.
        "search": 6,
        "config": 7,
    }


def test_operational_insights_slots_match_the_shared_axis():
    """A slot must mean the same category in every block.

    That is what lets an id decode to package-by-block and category-by-offset.
    A package omits the slots it has no tools for; it never renumbers.
    """
    oi = catalog.CATEGORY_SLOTS["operational_insights"]
    # `cluster` carries no OI tool; it is allocated because it is the
    # fail-closed category and every package must be able to resolve it.
    assert oi == {"cluster": 0, "schema": 1, "query": 3, "index": 4, "config": 7}
    shared = catalog.CATEGORY_SLOTS["operational"]
    for category, slot in oi.items():
        assert shared[category] == slot, category


def test_operational_insights_ids_sit_in_its_own_block():
    """Same offsets, different block -- and never the operational numbers."""
    base = catalog.SERVICE_PACKAGE_BLOCKS["operational_insights"]
    assert base == 0x10000
    assert catalog.tool_call_event_id("query", "read", "operational_insights") == 65587
    assert catalog.tool_call_event_id("query", "write", "operational_insights") == 65619
    assert catalog.tool_call_event_id("config", "read", "operational_insights") == 65591
    # The collision this block exists to prevent: the same tool name in both
    # servers must never resolve to the same id.
    assert catalog.tool_call_event_id(
        "config", "read", "operational"
    ) != catalog.tool_call_event_id("config", "read", "operational_insights")


def test_retiring_a_category_does_not_move_its_neighbours(monkeypatch):
    """The reason the slot table is explicit rather than positional.

    Under positional indexing, removing ``index`` would have shifted
    ``performance`` from 61493 down to 61492 — silently renumbering a shipped
    event that customers already have SIEM rules for.
    """
    trimmed = {
        name: slot
        for name, slot in catalog.CATEGORY_SLOTS["operational"].items()
        if name != "index"
    }
    monkeypatch.setattr(catalog, "CATEGORY_SLOTS", {"operational": trimmed})
    assert catalog.tool_call_event_id("performance", "read", "operational") == 61493
    assert catalog.tool_call_event_id("query", "read", "operational") == 61491


def test_next_free_slot_takes_the_lowest_never_allocated(monkeypatch):
    monkeypatch.setattr(catalog, "RETIRED_SLOTS", {})
    # 0-7 are allocated, so 8 is the lowest never-allocated slot.
    assert catalog.next_free_slot("operational") == 8


def test_next_free_slot_skips_retired_slots(monkeypatch):
    """Retired slots are not offered, even when reuse would be safe.

    Reuse remains *permissible* for a slot that never emitted a record. It is
    simply never *automatic*: reuse has to be chosen with the evidence that the
    slot never emitted in front of you, and written into ``CATEGORY_SLOTS`` by
    hand. Handing retired slots back from a helper would make that the default
    path, and the day a slot *has* emitted the default would be wrong.
    """
    monkeypatch.setattr(catalog, "RETIRED_SLOTS", {"operational": {"withdrawn": 8}})
    # 0-7 are live and 8 is retired, so the helper must skip past it to 9.
    # Retiring a slot *above* the first free one would make this pass
    # whether or not RETIRED_SLOTS were consulted at all.
    assert catalog.next_free_slot("operational") == 9


def test_exhausted_block_is_an_explicit_error(monkeypatch):
    monkeypatch.setattr(
        catalog,
        "CATEGORY_SLOTS",
        {"operational": {f"c{n}": n for n in range(catalog.MAX_SLOT + 1)}},
    )
    monkeypatch.setattr(catalog, "RETIRED_SLOTS", {})
    with pytest.raises(ValueError, match="request its own block"):
        catalog.next_free_slot("operational")


def test_nothing_is_retired_in_a_catalogue_that_has_not_shipped():
    """No category has been withdrawn from a released catalogue.

    The table exists for the first withdrawal, not to carry development
    history: a slot that never reached a release never meant anything to an
    operator, so recording it as retired would only make a reader wonder which
    numbers they must avoid.
    """
    assert catalog.RETIRED_SLOTS == {}


def test_every_slot_in_the_block_is_allocated_contiguously():
    """0-7 are in use and 8-15 are free, with no gaps to explain."""
    slots = sorted(catalog.CATEGORY_SLOTS["operational"].values())
    assert slots == list(range(8))
    assert catalog.next_free_slot("operational") == 8


def test_slot_table_ids_are_the_shipped_numbers():
    """The point of the explicit slot table: every id pinned literally."""
    assert tool_call_event_id("cluster", "read", "operational") == 61488
    assert tool_call_event_id("performance", "read", "operational") == 61493
    assert tool_call_event_id("performance", "write", "operational") == 61525


def test_search_ids_are_the_shipped_numbers():
    """Pinned literally, like every other shipped id."""
    assert tool_call_event_id("search", "read", "operational") == 61494
    assert tool_call_event_id("search", "write", "operational") == 61526
