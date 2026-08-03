<!--
Thank you for contributing to the Couchbase MCP Server!
Please read CONTRIBUTING.md before submitting. Keep PRs small and focused —
one tool, one bug fix, or one refactor per PR.
-->

## Related Issue

<!-- Every PR must link to an issue (JIRA ticket for Couchbase internal contributors, GitHub issue for external). Required for new tools — the tool interface must be agreed in the issue before implementation. -->

Resolves:

## What does this change do?

## Why is this change needed?

## Evidence of Testing

<!-- PRs without testing evidence will be sent back before review. See "Evidence of testing" in CONTRIBUTING.md. -->

**Automated tests** — commands run and results summary:

```
# e.g. uv run pytest tests/unit/  →  142 passed
# e.g. uv run pytest tests/integration/  →  38 passed, 2 skipped
```

**Environments tested** (both are required):

- [ ] Couchbase Capella (version: ____)
- [ ] Self-managed Couchbase Server (version: ____, e.g. via Docker)

**Manual verification** — MCP client used (Claude Desktop, Cursor, MCP Inspector, ...) and what was exercised. Screenshots or tool-call transcripts are very helpful:

## Compatibility Considerations

<!-- Note any impact on: Capella vs. self-managed behavior, managed MCP interfaces (cb_mcp.core), existing tool names/parameters/return shapes, CLI flags, environment variables, or new dependencies. Write "None" if not applicable. -->

## Checklist

- [ ] Linked to an issue (required for new tools). The issue can be on JIRA (preferred for internal contributors) or GitHub.
- [ ] Uses the Couchbase SDK (REST fallback justified in the description, if any)
- [ ] Works on both Capella and self-managed Couchbase Server
- [ ] No changes to `cb_mcp.core` contracts / managed MCP interfaces (or discussed first)
- [ ] Unit tests added/updated
- [ ] Integration tests added/updated (for cluster-touching changes)
- [ ] Read-only mode and tool annotations handled (for new/changed tools)
- [ ] Evidence of testing included above
- [ ] Docs updated (README, DOCKER.md) for user-facing changes
- [ ] Lint and pre-commit pass
