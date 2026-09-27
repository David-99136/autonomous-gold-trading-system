## Agent skills

### Issue tracker

Issues and specs are tracked as local Markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The repository uses the five default triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

The repository uses a single-context domain documentation layout. See `docs/agents/domain.md`.

### Multi-Agent Co-Authoring Convention (Antigravity & Codex)

The user alternates between **Antigravity** and **Codex** across token boundaries:
- **New files**: Add an author tag at the top of the module or docstring, e.g. `[Author: Antigravity | Date: YYYY-MM-DD]` or `[Author: Codex | Date: YYYY-MM-DD]`.
- **Modifications**: Tag significant modifications or new functions/fields with `# [Antigravity | YYYY-MM-DD]` or `# [Codex | YYYY-MM-DD]`.
- **Preserve integrity**: Never overwrite, strip, or rename previous agent's tags, comments, docstrings, or safety invariants without explicit user direction.
- **Fail-closed & offline tests**: Both agents strictly respect fail-closed invariants, Decimal pricing, and zero-network default in test suites.

