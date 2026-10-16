<!--
Thank you for contributing to the Couchbase MCP Server!
Please read CONTRIBUTING.md before submitting. Keep PRs small and focused —
one bug fix, one refactor, or one tool (or a small set of
closely related tools) per PR.
-->

## Related Issue

<!-- Every PR must link to an issue (JIRA ticket for Couchbase internal contributors, GitHub issue for external). Required for new tools — the tool interface must be agreed in the issue before implementation. -->

Resolves:

## Server(s) Affected

<!-- Tick every server whose behaviour changes. Shared machinery (cb_mcp/core, audit, shared utils) affects all servers. -->

- [ ] Operational (`operational`)
- [ ] Operational Insights (`operational-insights`)
- [ ] Shared machinery (all servers)

## What does this change do?

## Why is this change needed?

## Evidence of Testing

<!-- PRs without testing evidence will be sent back before review. See "Evidence of testing" in CONTRIBUTING.md. -->

**Automated tests** — commands run and results summary:

```
# e.g. uv run pytest tests/unit/  →  142 passed
# e.g. uv run pytest tests/integration/  →  38 passed, 2 skipped
```

**Environments tested** (see "Evidence of testing" in CONTRIBUTING.md for what each server requires):

- [ ] Operational: Couchbase Capella (version: ____)
- [ ] Operational: Self-managed Couchbase Server (version: ____, e.g. via Docker)
- [ ] Operational Insights: self-managed Operational Insights cluster (version: ____)

**Manual verification** — server and MCP client used (Claude Desktop, Cursor, MCP Inspector, ...) and what was exercised. Screenshots or tool-call transcripts are very helpful:

## Compatibility Considerations

<!-- Note any impact on: deployment support (Capella / self-managed), managed MCP interfaces (cb_mcp.core), existing tool names/parameters/return shapes, CLI flags, environment variables, or new dependencies. Write "None" if not applicable. -->

## Checklist

- [ ] Linked to an issue (required for new tools). The issue can be on JIRA (preferred for internal contributors) or GitHub.
- [ ] Uses the server's SDK (REST fallback justified in the description, if any)
- [ ] Works on every deployment the server targets (operational: Capella + self-managed, or declared in `TOOL_DEPLOYMENT_REQUIREMENTS` with a runtime check; Operational Insights: self-managed)
- [ ] No changes to `cb_mcp.core` contracts / managed MCP interfaces (or discussed first)
- [ ] Unit tests added/updated (across all servers for shared-machinery changes)
- [ ] Integration tests added/updated (for cluster-touching changes)
- [ ] Read-only mode, tool annotations, and audit classification handled (for new/changed tools)
- [ ] Results are bounded and no new cross-call state was added without discussion
- [ ] Evidence of testing included above
- [ ] Docs updated (README, DOCKER.md, `server.json`) for user-facing changes
- [ ] Lint and pre-commit pass
