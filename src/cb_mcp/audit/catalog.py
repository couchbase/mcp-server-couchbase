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

Two catalogue entries named in the PRD are deliberately absent:

* *tool invocation blocked (disabled)* — withheld and disabled tools are never
  registered with FastMCP, so the model is never told they exist and there is
  no refusal to record.
* *session terminated* — FastMCP 3.x exposes no session-end hook and a stdio
  client simply closes the pipe, so the event cannot be emitted reliably.
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

#: Tier-2 block for the Operational Insights (analytics) service package.
OPERATIONAL_INSIGHTS_BLOCK = 0x10000  # 65536

#: ``service package -> Tier-2 block base``. Blocks are 0x1000 apart. Adding a
#: package means an entry here, an entry in :data:`CATEGORY_SLOTS`, a
#: classification table in :mod:`cb_mcp.audit.classification`, and
#: ``audit_package`` on its ``ServerSpec`` — and renumbers nothing that already
#: exists.
#:
#: The AI Data Plane block is deliberately **not** listed: a package with no
#: tools would otherwise look audited to :func:`tool_call_event`, and a block
#: present here but absent from :data:`CATEGORY_SLOTS` is a half-registered
#: package waiting to raise at runtime. Its number is reserved by the constant
#: above instead, which is a comment on paper rather than a promise in code.
SERVICE_PACKAGE_BLOCKS: dict[str, int] = {
    "operational": OPERATIONAL_BLOCK,
    "operational_insights": OPERATIONAL_INSIGHTS_BLOCK,
}

#: Relative offset of the first read event inside a Tier-2 block.
READ_OFFSET = 48

#: Relative offset of the first event of an **unsplit** category, which has one
#: event rather than a read/write pair. Its own band, between the read and write
#: bands, so the offset keeps saying what the id is: 48-63 read, 64-79 unsplit,
#: 80-95 write. Putting an unsplit event at the read offset instead would have
#: made a non-filterable event sit in the band a consumer reads as filterable,
#: and putting it at the write offset would have labelled a ``SELECT`` a write.
UNSPLIT_OFFSET = 64

#: A category's write event sits this far above its read event.
WRITE_DELTA = 32

#: Highest slot the intra-block template reserves for categories: reads occupy
#: ``base + 48 + n`` for ``n`` in 0..15, unsplit events ``base + 64 + n``, and
#: writes ``base + 80 + n``.
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
#: Every package instantiates the *same* axis, so a slot means the same
#: category in every block and an id decodes to package-by-block and
#: category-by-offset. A package omits the slots it has no tools for rather
#: than renumbering: Operational Insights has no kv or search surface, so those
#: slots simply stay empty in its block and keep their meaning.
CATEGORY_SLOTS: dict[str, dict[str, int]] = {
    "operational": {
        "cluster": 0,
        "schema": 1,
        "kv": 2,
        "query": 3,
        "index": 4,
        "performance": 5,
        # Contiguous across 0-7, with 8-15 free for new categories. A number
        # here is a wire contract: once a category has emitted records, its
        # slot can never mean anything else.
        "search": 6,
        "config": 7,
    },
    # Operational Insights. Same axis, its own block: the two servers collide
    # on tool names — `get_server_configuration_status`, `create_index`,
    # `list_indexes`, `get_collections_in_scope`, `get_schema_for_collection` —
    # so a shared table could not represent both, and booking one server's call
    # against the other's block would put records under ids that mean a
    # different server.
    "operational_insights": {
        # `cluster` carries no Operational Insights tool today. It is allocated
        # anyway because it is the fail-closed category: an unclassified tool
        # resolves to (cluster, write), and a package without the slot made
        # that resolution raise. The raise was swallowed by the middleware's
        # "auditing must never break a call" guard, so the call succeeded and
        # *nothing was recorded* -- silence in the one case fail-closed exists
        # to make loud. See FAILSAFE_SLOT_REQUIRED below.
        "cluster": 0,
        "schema": 1,
        "query": 3,
        "index": 4,
        "config": 7,
    },
}

#: The category every package must allocate, whatever its tool surface.
#:
#: Kept here rather than in :mod:`cb_mcp.audit.classification` because this is
#: the module that owns slot allocation, and the check below has to run where
#: the tables are defined. classification.FAILSAFE_CATEGORY must equal it; a
#: test asserts the two agree.
FAILSAFE_SLOT_REQUIRED = "cluster"

for _package, _slots in CATEGORY_SLOTS.items():
    if FAILSAFE_SLOT_REQUIRED not in _slots:  # pragma: no cover - import guard
        raise RuntimeError(
            f"Service package {_package!r} does not allocate the fail-closed "
            f"category {FAILSAFE_SLOT_REQUIRED!r}. An unclassified tool in that "
            "package would raise inside the audit middleware, which swallows the "
            "error, and the call would go unrecorded entirely."
        )

#: Slots withdrawn from a released catalogue — ``package -> {category -> n}``,
#: under the number the category held.
#:
#: Empty, because nothing has been withdrawn from a released catalogue. It
#: exists so the first withdrawal has somewhere to go, and so
#: :func:`next_free_slot` has something to consult.
#:
#: A withdrawn slot may be given to a new category **only** when no record was
#: ever emitted against it. A slot that has emitted must never be reused:
#: reallocating it would make an existing SIEM rule silently match a new,
#: unrelated event. :func:`next_free_slot` never offers a retired slot, so the
#: reuse has to be written into :data:`CATEGORY_SLOTS` by hand, with that fact
#: established.
RETIRED_SLOTS: dict[str, dict[str, int]] = {}

#: Categories that emit **one** event per call rather than a read/write pair.
#:
#: ``query`` and ``search`` record that an operation happened without saying
#: whether it read or wrote. Both cover surfaces where the distinction was
#: judged unreliable enough to be worth more as a single honest event than as
#: two that might be wrong: a SQL++ or Analytics statement's class depends on
#: the statement, which means either inspecting it -- a leading-keyword check
#: books a plain read behind a CTE as a write, and a full parse costs tens of
#: milliseconds on every call -- or guessing. Vector operations sit inside
#: these two categories and follow them.
#:
#: The single event takes :data:`UNSPLIT_OFFSET`, its own band, and is **not
#: filterable**. One event covers the category's writes as well as its reads,
#: so allowing it to be switched off would let an operator turn down read noise
#: and lose the record of a mutation with it. The cost is that this category's
#: noise cannot be reduced at all; ``CB_MCP_AUDIT_LOG_DISABLED_EVENTS`` refuses
#: these ids like any other non-filterable event.
#:
#: Because the band is its own, an id still says what it is without a lookup:
#: the read band is filterable, the unsplit and write bands are not. A category
#: occupies exactly one band, so its offsets in the other two stay unused --
#: ``query`` leaves 61491 and 61523 empty, ``search`` 61494 and 61526. Those
#: gaps are the shared axis working, not numbers waiting to be reclaimed:
#: filling them would put a category at one offset in one package's block and
#: a different offset in another's, and ``base + 48 + n`` would stop being
#: true.
#:
#: The ``filterable`` flag in ``descriptor.json`` remains the authority for a
#: consumer that would rather not decode offsets at all.
UNSPLIT_CATEGORIES: frozenset[str] = frozenset({"query", "search"})


def is_unsplit(category: str) -> bool:
    """True when ``category`` emits one event rather than a read/write pair."""
    return category in UNSPLIT_CATEGORIES


def categories_for(package: str) -> tuple[str, ...]:
    """Category axis for ``package``'s Tier-2 block, ordered by slot.

    Derived from :data:`CATEGORY_SLOTS` so the two can never disagree.
    """
    return tuple(
        name
        for name, _ in sorted(CATEGORY_SLOTS[package].items(), key=lambda item: item[1])
    )


def category_index(category: str, package: str) -> int:
    """Return the slot ``n`` for ``category`` within ``package``'s block."""
    try:
        return CATEGORY_SLOTS[package][category]
    except KeyError as exc:  # pragma: no cover - guarded by classification
        raise ValueError(
            f"Unknown audit category {category!r} for service package "
            f"{package!r}; expected one of {categories_for(package)}."
        ) from exc


def next_free_slot(package: str) -> int:
    """Return the slot a newly added category should take.

    Returns the lowest **never-allocated** slot. Retired slots are never
    offered, even the ones :data:`RETIRED_SLOTS` marks as safe to reuse.

    Reuse is permissible when the slot has never emitted a record, but it is
    never automatic: it has to be chosen with that fact established, because
    handing back a slot that *has* emitted would make an existing SIEM rule
    silently match an unrelated event.

    Raises:
        ValueError: when the block's category template is exhausted. A new
            service package should then take a fresh 0x1000 block rather than
            crowding this one.
    """
    taken = set(CATEGORY_SLOTS[package].values()) | set(
        RETIRED_SLOTS.get(package, {}).values()
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


def tool_call_event_id(category: str, operation_class: str, package: str) -> int:
    """Resolve the Tier-2 event ID for a tool call.

    Args:
        category: A category allocated in ``package``'s block.
        operation_class: ``"read"`` or ``"write"``.
    """
    if operation_class not in ("read", "write"):
        raise ValueError(
            f"operation_class must be 'read' or 'write', got {operation_class!r}."
        )
    block = SERVICE_PACKAGE_BLOCKS[package]
    slot = category_index(category, package)

    # An unsplit category has one id, in its own band, so the operation class
    # cannot move it. Returning that id rather than raising on a "write" keeps
    # every caller that still passes a class working, including the fail-closed
    # path, which asks for a write and must still get an id. The read and write
    # offsets for such a category are simply never issued.
    if is_unsplit(category):
        return block + UNSPLIT_OFFSET + slot

    base = block + READ_OFFSET + slot
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

        An **unsplit** category is never filterable, whatever its class says.
        Its one event covers the writes in the category as well as the reads,
        so making it filterable would let an operator who only wanted less
        noise switch off the record of a mutation -- the one thing the
        read/write split existed to prevent. Filterability therefore cannot be
        inferred from the id's offset for these categories; the ``filterable``
        flag in ``descriptor.json`` is the authority.
        """
        return (
            self.operation_class == "read" and self.category not in UNSPLIT_CATEGORIES
        )


#: Human-readable names for the tool-call categories, used to build the
#: ``name`` field. Keys must cover every category in :data:`CATEGORY_SLOTS`.
_CATEGORY_LABELS: dict[str, str] = {
    "cluster": "cluster",
    "schema": "schema",
    "kv": "document",
    "query": "query",
    "index": "index",
    "performance": "performance",
    # Labels are shared across packages, so a slot reads the same wherever it
    # appears. "config" covers the server's own status and configuration
    # surface, which is MCP-level rather than cluster-level.
    "config": "config",
    "search": "search",
}


def tool_call_event(category: str, operation_class: str, package: str) -> ToolCallEvent:
    """Build the :class:`ToolCallEvent` for a category and operation class.

    For an unsplit category the class is normalised to ``"read"``. It carries no
    meaning there -- one id serves every call -- and leaving the caller's value
    in place would let a record claim ``filterable=False`` while holding the
    read-offset id that :data:`FILTERABLE_IDS` lists.
    """
    label = _CATEGORY_LABELS[category]
    article = "An" if label[0].lower() in "aeiou" else "A"

    if is_unsplit(category):
        return ToolCallEvent(
            id=tool_call_event_id(category, operation_class, package),
            event_name=label,
            description=f"{article} {label} operation was requested through a tool",
            category=category,
            operation_class="read",
        )

    verb = "read" if operation_class == "read" else "write"
    return ToolCallEvent(
        id=tool_call_event_id(category, operation_class, package),
        event_name=f"{label} {verb}",
        description=f"{article} {label} {verb} operation was requested through a tool",
        category=category,
        operation_class=operation_class,
    )


def all_tool_call_events(
    package: str,
) -> list[ToolCallEvent]:
    """Every Tier-2 tool-call entry for ``package``, for descriptor generation.

    An unsplit category contributes one entry. The descriptor is what a SIEM
    author reads to know which ids a build can emit, so listing the write id of
    an unsplit category would advertise an event that can never arrive.
    """
    return [
        tool_call_event(category, operation_class, package)
        for category in categories_for(package)
        for operation_class in (
            ("read",) if is_unsplit(category) else ("read", "write")
        )
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
    # Every package's Tier-2 events, not just the default one. The descriptor
    # is what a SIEM author reads to know which ids this build can emit, and a
    # deployment commonly runs both servers — shipping only one block's ids
    # would make the other server's records look unknown.
    #
    # ``service_package`` is carried on each entry because the tool-call names
    # repeat across blocks: "query" is 61507 in operational and 65603 in
    # operational_insights, and the name alone cannot tell them apart.
    entries += [
        {
            "id": event.id,
            "name": event.event_name,
            "description": event.description,
            "category": "tool_call",
            "service_package": package,
            "default_enabled": True,
            "filterable": event.filterable,
        }
        for package in SERVICE_PACKAGE_BLOCKS
        for event in all_tool_call_events(package)
    ]
    entries.sort(key=lambda entry: entry["id"])
    return {
        # No single `service_package`: the descriptor covers every registered
        # package, and naming one at the top of the document would be wrong
        # for every server running another. Each tool-call entry names its own.
        "service_packages": sorted(SERVICE_PACKAGE_BLOCKS),
        "blocks": {"core": CORE_BLOCK, **SERVICE_PACKAGE_BLOCKS},
        "events": entries,
    }


#: Ids of events an operator is permitted to suppress via configuration.
#:
#: Spans every registered package. A process runs one package, but an operator
#: writing CB_MCP_AUDIT_LOG_DISABLED_EVENTS should not have an id rejected as
#: unknown merely because this process happens to serve a different package —
#: the same configuration is routinely shared across both servers.
FILTERABLE_IDS: frozenset[int] = frozenset(
    [event.id for event in AuditEvent if event.filterable]
    + [
        event.id
        for package in SERVICE_PACKAGE_BLOCKS
        for event in all_tool_call_events(package)
        if event.filterable
    ]
)

#: Every known event id, across every registered package.
ALL_IDS: frozenset[int] = frozenset(
    [event.id for event in AuditEvent]
    + [
        event.id
        for package in SERVICE_PACKAGE_BLOCKS
        for event in all_tool_call_events(package)
    ]
)


__all__ = [
    "ALL_IDS",
    "CATEGORY_SLOTS",
    "SERVICE_PACKAGE_BLOCKS",
    "categories_for",
    "MAX_SLOT",
    "RETIRED_SLOTS",
    "UNSPLIT_CATEGORIES",
    "UNSPLIT_OFFSET",
    "is_unsplit",
    "CORE_BLOCK",
    "FILTERABLE_IDS",
    "OPERATIONAL_BLOCK",
    "READ_OFFSET",
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
