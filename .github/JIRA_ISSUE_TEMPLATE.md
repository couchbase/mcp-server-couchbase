# MCP Server Issue Template

Copy the template below into the description of your JIRA ticket (Couchbase internal contributors) or GitHub issue (external contributors) when proposing a change to the Couchbase MCP Server. Every pull request must link to an issue based on this template — see [CONTRIBUTING.md](../CONTRIBUTING.md).

For new tools, use **one issue per class of related tools** (e.g., one issue for a set of Search tools), not one issue per tool.

---

## Summary

<!-- One or two sentences describing the proposed change. -->

**Type of change:** New tool(s) / Enhancement / Bug fix / Refactor / Documentation

## Use Case

<!--
Be as detailed as possible. This is the most important section — it decides
whether the change is accepted. Cover:
- What task can an AI agent NOT accomplish with the existing tools today?
- Who needs this and in what scenario (development, operations, analytics, ...)?
- A concrete example: the prompt a user would give, and the sequence of tool
  calls the agent would make to complete it with the proposed tools.
-->

**Example prompt / workflow:**

> _"Example user prompt to the agent"_
>
> 1. Agent calls `proposed_tool_name(param=...)`
> 2. Agent uses the result to ...

## Proposed Tool Interface(s)

<!-- Repeat this block for each tool in the class. For non-tool changes, describe the interface impact (CLI flags, env vars, config) instead. -->

### `tool_name`

- **Description** (shown to the LLM):
- **Parameters**: name, type, required/optional, description
- **Return shape**: fields and types; how large can the result get, and how is its size bounded (e.g., a `limit` parameter, as existing tools use)?
- **Classification**: `READ_ONLY_TOOLS` or `KV_WRITE_TOOLS` (behavior under read-only mode)
- **Annotations**: `readOnlyHint` / `idempotentHint` / `destructiveHint`
- **Confirmation**: does this tool need confirmation/elicitation before executing?

## Feasibility & Deployment

- **Couchbase Python SDK support**: which SDK APIs will be used? If the SDK lacks the capability, describe the REST fallback and why it's needed.
- **Capella**: available on Capella? Any limitations?
- **Self-managed Couchbase Server**: available on the server versions supported by the MCP server? Any minimum version or service requirements (Query, Index, Search, ...)?
- **RBAC**: what permissions does the database user need?

## Compatibility Impact

- **Managed MCP interfaces**: does this touch `cb_mcp.core` contracts (`ClusterProvider`, ...) or require host-specific behavior? (Should normally be "No")
- **Backwards compatibility**: any changes to existing tool names/parameters/return shapes, CLI flags, or environment variables?
- **New dependencies**: any new runtime dependencies? (Requires discussion — see CONTRIBUTING.md)

## Testing Plan

- **Unit tests**: what will be covered with fakes/mocks?
- **Integration tests**: what will be verified against live clusters (both Capella and self-managed)?
- **Accuracy tests**: for new tools, which tool-calling cases will be added under `tests/accuracy/`?

## Delivery Plan

<!-- How will the work be split into small, reviewable PRs? e.g.,
1. PR 1: tool skeleton + unit tests for tool A
2. PR 2: tool B + integration tests
3. PR 3: accuracy cases + docs
-->
