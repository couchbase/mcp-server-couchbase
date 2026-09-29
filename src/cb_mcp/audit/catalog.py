"""Audit event catalogue.

Two tiers, following the PRD and Couchbase Server / Sync Gateway precedent:

* **Tier 1 — core block ``0xE000`` (57344).** Lifecycle, session, authorization
  and guardrail events. Identical regardless of which service sub-package is
  loaded: a denied scope check is the same event whether it gated a KV tool or
  a query tool.
* **Tier 2 — one block per service package.** The operational package owns
  ``0xF000`` (61440). Within a Tier-2 block the layout is a reusable template:
  reads at ``base + 48 + n`` and writes at ``read + 32`` for category index
  ``n``, so the same relative offset means the same category in every package.

The ID space is treated as frozen. Adding a tool requires only a new entry in
:mod:`cb_mcp.audit.classification` — no new event ID, and no change to a SIEM
rule.

Events defined in code are the source of truth for the server. ``descriptor.json``
ships the same catalogue for operators and SIEM authors;
``tests/unit/test_audit_catalog.py`` asserts the two never drift.

Two catalogue entries from the PRD draft are deliberately absent:

* *tool invocation blocked (disabled)* — withheld and disabled tools are never
  registered with FastMCP, so the model is never told they exist and there is
  no refusal to record. Removed by product decision on 2026-08-25.
* *session terminated* — FastMCP 3.x exposes no session-end hook and a stdio
  client simply closes the pipe, so the event could never be emitted reliably.
  On hold by product decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ---------------------------------------------------------------------------
# Block layout
# ---------------------------------------------------------------------------

#: Tier-1 shared core block.
CORE_BLOCK = 0xE000  # 57344

#: Tier-2 block for the operational service package (kv, query, index, ...).
OPERATIONAL_BLOCK = 0xF000  # 61440

#: Service package this build audits by default, and the only one today.
#:
#: The repository ships two servers. Only ``operational`` is audited: the
#: Operational Insights server's tool surface is new and still moving, and a
#: Tier-2 block, once records exist against it, can never be renumbered. Its
#: block is therefore left unallocated rather than spent early.
DEFAULT_SERVICE_PACKAGE = "operational"

#: ``service package -> Tier-2 block base``. Blocks are 0x1000 apart, so the
#: next package takes 0x10000. Adding one means an entry here, an entry in
#: :data:`CATEGORY_SLOTS`, a classification table in
#: :mod:`cb_mcp.audit.classification`, and ``audit_package`` on its
#: ``ServerSpec`` — and renumbers nothing that already exists.
SERVICE_PACKAGE_BLOCKS: dict[str, int] = {
    "operational": OPERATIONAL_BLOCK,
}

#: Relative offset of the first read event inside a Tier-2 block.
READ_OFFSET = 48

#: A category's write event sits this far above its read event.
WRITE_DELTA = 32

#: Highest slot the intra-block template reserves for categories: reads occupy
#: ``base + 48 + n`` for ``n`` in 0..15, writes ``base + 80 + n``.
MAX_SLOT = 15

#: Explicit slot allocation for Tier-2 categories — ``category -> n``, where a
#: read event id is ``base + READ_OFFSET + n``.
#:
#: Deliberately a table keyed by name rather than the position of a name in a
#: list. Positional indexing reads more tidily but means retiring one category
#: silently renumbers every category after it, and **a shipped audit event id
#: can never be renumbered**: a SIEM rule written against the old number would
#: then match a different event. With an explicit table a slot keeps its number
#: for as long as the catalogue exists, whatever happens around it.
#:
#: Add a category by giving it the next free slot (:func:`next_free_slot`).
#: Never change a number already in this table.
CATEGORY_SLOTS: dict[str, dict[str, int]] = {
    "operational": {
        "cluster": 0,
        "schema": 1,
        "kv": 2,
        "query": 3,
        "index": 4,
        "performance": 5,
        # Slots 6 and 7 are retired (see RETIRED_SLOTS) and deliberately left
        # unused, so search took the next never-allocated slot instead of
        # reusing one. Reuse was permitted by the rule below — neither retired
        # slot ever emitted a record — but declined: two dead numbers cost
        # nothing in a 16-slot block, and a slot that has meant two different
        # things is a trap for anyone reading old rules.
        "search": 8,
    },
}

#: Slots retired from use, kept here so their history is visible — ``category
#: -> n``, using the number the category held.
#:
#: A retired slot may be reallocated to a new category **only** when no audit
#: record was ever emitted against it. That holds for a category retired
#: because it never had any tools classified to it: with no tools there were no
#: records, so no SIEM rule for the old meaning can exist and reuse is safe.
#:
#: A slot that has ever emitted a record must never be reused, even after the
#: category is withdrawn — reallocating it would make an old rule silently
#: match a new, unrelated event. Record such a slot here and leave it out of
#: :func:`next_free_slot`'s reusable set.
RETIRED_SLOTS: dict[str, dict[str, int]] = {
    "operational": {
        # Retired before release: no tool was ever classified to either
        # category, so neither slot ever emitted a record and both would be
        # safe to reallocate. Left unallocated by choice when the search
        # category was added on 2026-09-29 — see the note in CATEGORY_SLOTS.
        "vector": 6,
        "analytics": 7,
    },
}

#: Retired slots that must never be reallocated because records exist for them.
PERMANENTLY_RESERVED_SLOTS: frozenset[int] = frozenset()


def categories_for(package: str = DEFAULT_SERVICE_PACKAGE) -> tuple[str, ...]:
    """Category axis for ``package``'s Tier-2 block, ordered by slot.

    Derived from :data:`CATEGORY_SLOTS` so the two can never disagree.
    """
    return tuple(
        name
        for name, _ in sorted(CATEGORY_SLOTS[package].items(), key=lambda item: item[1])
    )


#: Category axis of the default package, kept as a module constant because
#: it is the axis every descriptor and every test refers to today.
CATEGORIES: tuple[str, ...] = categories_for()

#: Name of the service package this build audits. Recorded once at startup
#: rather than repeated on every record.
SERVICE_PACKAGE = DEFAULT_SERVICE_PACKAGE


def category_index(category: str, package: str = DEFAULT_SERVICE_PACKAGE) -> int:
    """Return the slot ``n`` for ``category`` within ``package``'s block."""
    try:
        return CATEGORY_SLOTS[package][category]
    except KeyError as exc:  # pragma: no cover - guarded by classification
        raise ValueError(
            f"Unknown audit category {category!r} for service package "
            f"{package!r}; expected one of {categories_for(package)}."
        ) from exc


def next_free_slot(package: str = DEFAULT_SERVICE_PACKAGE) -> int:
    """Return the slot a newly added category should take.

    Returns the lowest **never-allocated** slot. Retired slots are deliberately
    not offered, even the ones :data:`RETIRED_SLOTS` records as safe to reuse:
    when the search category was added on 2026-09-29 the reusable slots 6 and 7
    were declined in favour of slot 8, on the grounds that two dead numbers cost
    nothing in a 16-slot block while a slot that has meant two different things
    is a trap for anyone reading an old rule. Reuse remains permissible, but it
    is a decision someone must take deliberately rather than inherit from a
    helper.

    Raises:
        ValueError: when the block's category template is exhausted. A new
            service package should then take a fresh 0x1000 block rather than
            crowding this one.
    """
    taken = (
        set(CATEGORY_SLOTS[package].values())
        | set(RETIRED_SLOTS.get(package, {}).values())
        | PERMANENTLY_RESERVED_SLOTS
    )
    for slot in range(MAX_SLOT + 1):
        if slot not in taken:
            return slot
    raise ValueError(
        f"All {MAX_SLOT + 1} category slots in the {package!r} block are "
        "allocated or retired. A new service package should request its own "
        "block; reusing a retired slot is possible but must be decided "
        "explicitly — see RETIRED_SLOTS."
    )


def tool_call_event_id(
    category: str, operation_class: str, package: str = DEFAULT_SERVICE_PACKAGE
) -> int:
    """Resolve the Tier-2 event ID for a tool call.

    Args:
        category: One of :data:`CATEGORIES`.
        operation_class: ``"read"`` or ``"write"``.
    """
    if operation_class not in ("read", "write"):
        raise ValueError(
            f"operation_class must be 'read' or 'write', got {operation_class!r}."
        )
    base = (
        SERVICE_PACKAGE_BLOCKS[package]
        + READ_OFFSET
        + category_index(category, package)
    )
    return base + WRITE_DELTA if operation_class == "write" else base


class AuditEvent(Enum):
    """Catalogue entry.

    Value is the numeric event ``id``; the remaining members carry the
    descriptor metadata that ships to operators.
    """

    # -- Core: lifecycle (57344-57359) ------------------------------------
    SERVER_STARTED = (
        CORE_BLOCK + 0,
        "server started",
        "MCP server started; carries the static server context",
        "lifecycle",
        False,
    )
    SERVER_STOPPED = (
        CORE_BLOCK + 1,
        "server stopped",
        "MCP server shut down cleanly",
        "lifecycle",
        False,
    )
    SERVER_CONFIGURATION = (
        CORE_BLOCK + 2,
        "server configuration",
        (
            "Resolved server configuration recorded at startup, including "
            "read-only mode and the withheld and disabled tool sets"
        ),
        "lifecycle",
        False,
    )

    # -- Core: session (57360-57375) --------------------------------------
    SESSION_INITIALIZED = (
        CORE_BLOCK + 16,
        "session initialized",
        "MCP initialize handshake completed",
        "session",
        True,
    )

    # -- Core: authorization (57376-57391) --------------------------------
    TOKEN_REJECTED = (
        CORE_BLOCK + 32,
        "token rejected",
        "Bearer JWT rejected during verification",
        "authorization",
        False,
    )
    SCOPE_CHECK_DENIED = (
        CORE_BLOCK + 33,
        "scope check denied",
        "A required OAuth scope was not granted by the presented token",
        "authorization",
        False,
    )

    # -- Core: guardrail (57488-57503) ------------------------------------
    WRITE_BLOCKED_READ_ONLY = (
        CORE_BLOCK + 144,
        "write blocked (read-only mode)",
        "A SQL++ statement modifying data or structure was refused because "
        "CB_MCP_READ_ONLY_MODE is enabled",
        "guardrail",
        False,
    )
    CONFIRMATION_DECLINED = (
        CORE_BLOCK + 146,
        "confirmation declined",
        "The user rejected an elicitation for a confirmation-required tool",
        "guardrail",
        False,
    )
    CONFIRMATION_SKIPPED = (
        CORE_BLOCK + 147,
        "confirmation skipped",
        "A confirmation-required tool executed without confirmation because "
        "the client does not support elicitation",
        "guardrail",
        False,
    )

    def __init__(
        self,
        event_id: int,
        event_name: str,
        description: str,
        category: str,
        filterable: bool,
    ) -> None:
        self.id = event_id
        self.event_name = event_name
        self.description = description
        self.category = category
        self.filterable = filterable

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditEvent {self.id} {self.event_name!r}>"


@dataclass(frozen=True)
class ToolCallEvent:
    """A resolved Tier-2 tool-call catalogue entry.

    Built on demand from a category and operation class rather than enumerated,
    because the specific tool lives in ``tool_name`` and one ID serves a whole
    category.
    """

    id: int
    event_name: str
    description: str
    category: str
    operation_class: str

    @property
    def filterable(self) -> bool:
        """Reads are filterable per category; writes never are.

        An operator can turn read noise down but cannot switch off the record
        of who changed what.
        """
        return self.operation_class == "read"


#: Human-readable names for the tool-call categories, used to build the
#: ``name`` field. Keys must cover :data:`CATEGORIES`.
_CATEGORY_LABELS: dict[str, str] = {
    "cluster": "cluster",
    "schema": "schema",
    "kv": "document",
    "query": "query",
    "index": "index",
    "performance": "performance",
    "search": "search",
}


def tool_call_event(
    category: str, operation_class: str, package: str = DEFAULT_SERVICE_PACKAGE
) -> ToolCallEvent:
    """Build the :class:`ToolCallEvent` for a category and operation class."""
    label = _CATEGORY_LABELS[category]
    verb = "read" if operation_class == "read" else "write"
    article = "An" if label[0].lower() in "aeiou" else "A"
    return ToolCallEvent(
        id=tool_call_event_id(category, operation_class, package),
        event_name=f"{label} {verb}",
        description=f"{article} {label} {verb} operation was requested through a tool",
        category=category,
        operation_class=operation_class,
    )


def all_tool_call_events(
    package: str = DEFAULT_SERVICE_PACKAGE,
) -> list[ToolCallEvent]:
    """Every Tier-2 tool-call entry for ``package``, for descriptor generation."""
    return [
        tool_call_event(category, operation_class, package)
        for category in categories_for(package)
        for operation_class in ("read", "write")
    ]


def build_descriptor() -> dict:
    """Build the descriptor document describing the whole catalogue."""
    entries = [
        {
            "id": event.id,
            "name": event.event_name,
            "description": event.description,
            "category": event.category,
            "default_enabled": True,
            "filterable": event.filterable,
        }
        for event in AuditEvent
    ]
    entries += [
        {
            "id": event.id,
            "name": event.event_name,
            "description": event.description,
            "category": "tool_call",
            "default_enabled": True,
            "filterable": event.filterable,
        }
        for event in all_tool_call_events()
    ]
    entries.sort(key=lambda entry: entry["id"])
    return {
        "service_package": SERVICE_PACKAGE,
        "blocks": {"core": CORE_BLOCK, **SERVICE_PACKAGE_BLOCKS},
        "events": entries,
    }


#: Every event name that may appear in a record, mapped to its numeric id.
EVENT_NAMES_TO_IDS: dict[str, int] = {
    **{event.event_name: event.id for event in AuditEvent},
    **{event.event_name: event.id for event in all_tool_call_events()},
}

#: Ids of events an operator is permitted to suppress via configuration.
FILTERABLE_IDS: frozenset[int] = frozenset(
    [event.id for event in AuditEvent if event.filterable]
    + [event.id for event in all_tool_call_events() if event.filterable]
)

#: Every known event id.
ALL_IDS: frozenset[int] = frozenset(EVENT_NAMES_TO_IDS.values())


__all__ = [
    "ALL_IDS",
    "CATEGORIES",
    "CATEGORY_SLOTS",
    "DEFAULT_SERVICE_PACKAGE",
    "SERVICE_PACKAGE_BLOCKS",
    "categories_for",
    "MAX_SLOT",
    "PERMANENTLY_RESERVED_SLOTS",
    "RETIRED_SLOTS",
    "CORE_BLOCK",
    "EVENT_NAMES_TO_IDS",
    "FILTERABLE_IDS",
    "OPERATIONAL_BLOCK",
    "READ_OFFSET",
    "SERVICE_PACKAGE",
    "WRITE_DELTA",
    "AuditEvent",
    "ToolCallEvent",
    "all_tool_call_events",
    "build_descriptor",
    "category_index",
    "next_free_slot",
    "tool_call_event",
    "tool_call_event_id",
]
