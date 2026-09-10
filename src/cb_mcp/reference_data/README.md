# Reference datasets

Each `*.jsonl` file here is a **reference dataset**: a lookup table of the valid input values for
some MCP tool. The `discover_tool_input_values` tool searches them so the calling LLM can find an
exact identifier (a metric name, an error code, a function name) instead of guessing one or being
told to go read a documentation website.

**Adding a dataset is a data change, not a code change.** Drop a conformant `.jsonl` file in this
directory with `"tools": ["your_tool_name"]` in its envelope and it is live — no Python edit, no
registration list, no tool-count update.

## File format

JSON Lines. **Line 1 is the envelope. Every subsequent line is one record.**

JSONL rather than a JSON array because the server streams these files: it never holds a dataset in
memory, so search memory stays flat no matter how large the dataset grows. `json.loads` on a single
`[...]` would have to materialise every record before yielding the first one.

### Line 1 — the envelope

```json
{
  "schema_version": 1,
  "dataset_id": "couchbase_server_metrics",
  "title": "Couchbase Server metrics reference",
  "tools": ["get_cluster_metrics"],
  "source_url": "https://docs.couchbase.com/server/current/metrics-reference/metrics-reference.html",
  "generated_at": "2026-09-10",
  "record_count": 1140,
  "id_field": "name",
  "search_fields": [
    {"field": "name", "weight": 1.0, "split_underscores": true},
    {"field": "description", "weight": 0.85}
  ],
  "chapter_fields": ["category", "metric_type", "unit"],
  "null_labels": {"metric_type": "unspecified", "unit": "none"}
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | Format version. Currently `1`. |
| `dataset_id` | yes | Unique id for this dataset. Also accepted as a `tool_name`. |
| `title` | yes | Human-readable name, echoed back to the caller. |
| `tools` | yes | Tool names this dataset serves. A caller passing any of these gets this dataset. Must not collide with another dataset's `tools` or `dataset_id`. |
| `source_url` | yes | Where the data came from. |
| `generated_at` | yes | ISO date the file was produced. |
| `record_count` | yes | Number of record lines. Must match exactly. |
| `id_field` | yes | The field holding each record's identifier — the value a caller ultimately feeds into the target tool. Must be unique across records. |
| `search_fields` | yes | Which fields are searched, and how. See below. |
| `chapter_fields` | no | Browsable/filterable fields. See below. Omit for a search-only dataset. |
| `null_labels` | no | Per-chapter label to write when a value is missing, so chapter fields are never null. |

### Lines 2…N — the records

One JSON object per line. Fields are free-form beyond the envelope's requirements — whatever you
put in a record is what the caller gets back.

```
{"category": "Data Service Metrics", "description": "Total enqueued items on disk queue", "metric_type": "counter", "name": "kv_ep_diskqueue_fill", "since_version": "7.0.0", "source_url": "https://...", "unit": "none"}
```

Write keys in **sorted order, identical on every line**. Then regenerating the file produces a
line-by-line diff showing exactly which records changed, instead of a reindented blob.

## `search_fields`

Each entry names a field to match against:

- `field` — the record field to search.
- `weight` — multiplier on that field's score, `0.0`–`1.0`. Use `1.0` for the identifier and
  something lower for prose, so a name match outranks a description match.
- `split_underscores` — replace `_` with spaces before matching. Set this on identifier fields:
  matching is token-based, so `kv_ep_diskqueue_fill` is a single token until it's split, and
  keywords like `["disk", "queue"]` would never reach its parts.

A record's score is the **best** score across its search fields, not the sum.

## `chapter_fields` — the table of contents

Chapters are the small set of fields a caller can browse and filter on. They exist for
**correctness, not speed**: identifiers here use short prefixes (`kv_`, `n1ql_`, `fts_`) that no
keyword search can connect to the service names humans actually say, so an unfiltered search for
"query service memory" happily returns Index Service metrics. A `category` filter fixes that.

The chapter listing is inlined in *every* tool response, so it has to stay small. Enforced by
`tests/unit/test_reference_data_conformance.py`:

- **At most 3 chapter fields.**
- **At most 25 distinct chapter values in total**, summed across all chapter fields.
- **Never null** on any record — use `null_labels` to give missing values an explicit bucket name.
- Values and counts are **derived from the records**, never declared, so they cannot drift out of
  sync with the data.

**Chapters must be categorical, not ordinal.** `since_version` is the worked counter-example: it has
21 distinct values (which alone nearly exhausts the budget), and equality filtering is the wrong
question anyway — nobody wants "metrics added in exactly 7.6.1", they want "metrics available on
7.6+". It stays an ordinary returned field.

If a field's cardinality is too high, look for a coarser one. The metrics dataset originally had a
single compound `metric_type` (`"counter / bytes"`, `"gauge / seconds"`) with 20 distinct values;
splitting it into `metric_type` (4) and `unit` (10) brought the total to 24.

**Chapters are optional.** A flat dataset with no useful categorical axis can omit `chapter_fields`
entirely — browsing then returns a sample record and tells the caller to search instead.

## Checking your dataset

```bash
uv run --extra dev pytest tests/unit/test_reference_data_conformance.py -v
```

It runs over every `.jsonl` in this directory, so a new dataset is validated the moment you add it.
Failures name the file, the field, and the offending values.
