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
    CATEGORIES,
    CORE_BLOCK,
    OPERATIONAL_BLOCK,
    READ_OFFSET,
    WRITE_DELTA,
    AuditEvent,
    all_tool_call_events,
    build_descriptor,
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
    assert CATEGORIES == (
        "cluster",
        "schema",
        "kv",
        "query",
        "index",
        "performance",
        # Slot 7, revived from the retired `analytics` category on 2026-10-12
        # by product decision. Safe only because `analytics` never had a tool
        # classified to it and so never emitted a record; see RETIRED_SLOTS.
        "config",
        "search",
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
    assert tool_call_event_id(category, "read") == read_id
    assert tool_call_event_id(category, "write") == write_id
    assert write_id - read_id == WRITE_DELTA


def test_kv_events_use_the_document_label():
    # The PRD names the KV events "document read"/"document write" even though
    # the category axis calls the category "kv".
    assert tool_call_event("kv", "read").event_name == "document read"
    assert tool_call_event("kv", "write").event_name == "document write"


def test_unknown_category_and_class_are_rejected():
    with pytest.raises(ValueError, match="Unknown audit category"):
        tool_call_event_id("nonsense", "read")
    with pytest.raises(ValueError, match="must be 'read' or 'write'"):
        tool_call_event_id("kv", "sideways")


def test_writes_are_never_filterable_and_reads_always_are():
    for event in all_tool_call_events():
        assert event.filterable is (event.operation_class == "read")


def test_security_and_lifecycle_events_are_not_filterable():
    for event in AuditEvent:
        if event is AuditEvent.SESSION_INITIALIZED:
            assert event.filterable is True
        else:
            assert event.filterable is False, f"{event.event_name} must be mandated"


def test_ids_are_unique():
    ids = [event.id for event in AuditEvent] + [
        event.id for event in all_tool_call_events()
    ]
    assert len(ids) == len(set(ids))


def test_removed_and_deferred_events_are_absent():
    """Two PRD draft entries are deliberately not implemented.

    57489 (tool invocation blocked - disabled) was removed by product decision:
    withheld tools are never registered, so the model is never told they exist
    and there is no refusal to record. 57361 (session terminated) is on hold
    because FastMCP exposes no session-end hook.
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
    assert shipped["service_package"] == "operational"
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
    """``CATEGORIES`` is derived from the slot table, ordered by slot."""
    assert (
        tuple(
            name
            for name, _ in sorted(
                catalog.CATEGORY_SLOTS["operational"].items(), key=lambda kv: kv[1]
            )
        )
        == catalog.CATEGORIES
    )


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
        # Revived the retired `analytics` slot on 2026-10-12. Permitted
        # because that category never emitted a record, and taken as an
        # explicit decision -- next_free_slot still declines to offer it.
        "config": 7,
        # Added 2026-09-29 with the FTS tools. Slot 8, not the reusable
        # retired 6, so no number has ever meant two things.
        "search": 8,
    }


def test_operational_insights_slots_match_the_shared_axis():
    """A slot must mean the same category in every block.

    That is what lets an id decode to package-by-block and category-by-offset.
    A package omits the slots it has no tools for; it never renumbers.
    """
    oi = catalog.CATEGORY_SLOTS["operational_insights"]
    assert oi == {"schema": 1, "query": 3, "index": 4, "config": 7}
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
    assert catalog.tool_call_event_id("performance", "read") == 61493
    assert catalog.tool_call_event_id("query", "read") == 61491


def test_next_free_slot_takes_the_lowest_never_allocated(monkeypatch):
    monkeypatch.setattr(catalog, "RETIRED_SLOTS", {})
    monkeypatch.setattr(catalog, "PERMANENTLY_RESERVED_SLOTS", frozenset())
    # 0-5 and 8 are allocated, so 6 is the lowest never-allocated slot.
    assert catalog.next_free_slot() == 6


def test_next_free_slot_skips_retired_slots(monkeypatch):
    """Retired slots are not offered, even when reuse would be safe.

    Reuse remains *permissible* for a slot that never emitted a record — the
    rule in ``RETIRED_SLOTS`` still says so. It is simply not automatic. When
    the search category was added the reusable slot 6 was declined in favour of
    8, on the grounds that a number which has meant two different things is a
    trap for anyone reading an old rule. Handing retired slots back from a
    helper would make the trap the default path.
    """
    monkeypatch.setattr(catalog, "RETIRED_SLOTS", {"operational": {"vector": 6}})
    monkeypatch.setattr(catalog, "PERMANENTLY_RESERVED_SLOTS", frozenset())
    # 7 is live (config) and 8 is live (search), so the next never-allocated
    # slot is 9 -- the helper still refuses to offer the retired 6.
    assert catalog.next_free_slot() == 9


def test_permanently_reserved_slots_are_never_reallocated(monkeypatch):
    """A slot that has emitted records must not be handed to a new category."""
    monkeypatch.setattr(catalog, "RETIRED_SLOTS", {"operational": {"vector": 6}})
    monkeypatch.setattr(catalog, "PERMANENTLY_RESERVED_SLOTS", frozenset({6}))
    assert catalog.next_free_slot() == 9


def test_exhausted_block_is_an_explicit_error(monkeypatch):
    monkeypatch.setattr(
        catalog,
        "CATEGORY_SLOTS",
        {"operational": {f"c{n}": n for n in range(catalog.MAX_SLOT + 1)}},
    )
    monkeypatch.setattr(catalog, "RETIRED_SLOTS", {})
    monkeypatch.setattr(catalog, "PERMANENTLY_RESERVED_SLOTS", frozenset())
    with pytest.raises(ValueError, match="request its own block"):
        catalog.next_free_slot()


def test_retired_categories_are_gone_but_their_slots_are_recorded():
    """vector and analytics were retired before release.

    Neither ever had a tool classified to it, so neither slot ever emitted a
    record — which is precisely the condition that makes a slot safe to hand to
    a future category. The numbers stay recorded so the history is visible and
    nobody wonders why the live table starts skipping at 6.
    """
    assert "vector" not in catalog.CATEGORY_SLOTS["operational"]
    assert "analytics" not in catalog.CATEGORY_SLOTS["operational"]
    assert catalog.RETIRED_SLOTS == {"operational": {"vector": 6, "analytics": 7}}
    # Never emitted a record, so never permanently reserved.
    assert not catalog.PERMANENTLY_RESERVED_SLOTS

    for name in ("vector", "analytics"):
        with pytest.raises(ValueError, match="Unknown audit category"):
            catalog.tool_call_event_id(name, "read")


def test_retired_ids_are_absent_from_the_catalogue_and_descriptor():
    """Slot 6 stays dead: 61494 and 61526 must not resolve to anything.

    Slot 7 (61495/61527) was retired with `analytics` and later revived for
    `config`, which the rule permits only because `analytics` never emitted a
    record. Slot 6 was not revived, so its pair must still resolve to nothing
    -- and this test is what would catch it being handed out by accident.
    """
    retired = {61494, 61526}
    live = {event.id for event in all_tool_call_events()}
    assert live & retired == set()

    shipped = json.loads(DESCRIPTOR_PATH.read_text(encoding="utf-8"))
    assert {entry["id"] for entry in shipped["events"]} & retired == set()


def test_the_revived_slot_never_emitted_under_its_old_meaning():
    """The precondition for reusing slot 7, asserted rather than assumed.

    Reallocating a slot that had ever emitted a record would make an existing
    SIEM rule silently match a new, unrelated event. `analytics` was retired
    with no tool ever classified to it, so no record for 61495 can exist in
    anyone's logs -- which is the only reason `config` may have the number.
    """
    assert catalog.RETIRED_SLOTS["operational"]["analytics"] == 7
    assert catalog.CATEGORY_SLOTS["operational"]["config"] == 7
    # Permanently reserved slots are the ones that *did* emit; 7 is not one.
    assert 7 not in catalog.PERMANENTLY_RESERVED_SLOTS


def test_retirement_did_not_move_the_surviving_ids():
    """The point of the explicit slot table, asserted on the real retirement."""
    assert tool_call_event_id("cluster", "read") == 61488
    assert tool_call_event_id("performance", "read") == 61493
    assert tool_call_event_id("performance", "write") == 61525


def test_a_new_category_takes_the_next_never_allocated_slot():
    """Slots 6 and 7 stay retired; the next category gets 9, after search's 8."""
    assert catalog.next_free_slot() == 9


def test_search_ids_are_the_shipped_numbers():
    """Pinned literally, like every other shipped id.

    Search took slot 8 rather than the reusable retired slot 6, so these are
    61496/61528 and not 61494/61526.
    """
    assert tool_call_event_id("search", "read") == 61496
    assert tool_call_event_id("search", "write") == 61528
