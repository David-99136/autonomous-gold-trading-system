# Domain Docs

## Before exploring

Read the following when they exist:

- `CONTEXT.md` at the repository root
- `CONTEXT-MAP.md` if the repository later becomes multi-context
- Relevant architectural decisions under `docs/adr/`

Missing domain documents do not block work. They are created when terminology or
architectural decisions are actually established.

## Layout

This repository uses the single-context layout:

```text
/
├── CONTEXT.md
├── docs/
│   └── adr/
└── src/
```

## Vocabulary

Use terminology defined in `CONTEXT.md` consistently. If a necessary concept is
missing, record the gap instead of silently inventing competing terminology.

## Architectural conflicts

If proposed work contradicts an existing ADR, identify the conflict explicitly
rather than silently overriding the decision.
