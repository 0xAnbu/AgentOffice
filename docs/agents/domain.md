# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`GLOSSARY.md`** at the repo root.
- **`docs/DECISIONS.md`**: this repo's decision log (ADRs). Read the "Applicability today" table first, then the ADRs that touch the area you're about to work in. Several early ADRs are marked historical and superseded by later ones (e.g. ADR-101); follow the later decision.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

Single-context repo:

```
/
├── GLOSSARY.md
├── docs/
│   └── DECISIONS.md      ← all ADRs live here, as sections
└── agentlink/, admin/, …
```

## Recording a new decision

Don't create `docs/adr/NNNN-*.md` files. Append a new section to `docs/DECISIONS.md`, continuing the current numbering (ADR-1xx), using the existing format:

```markdown
### ADR-1NN · <title>: <accepted|proposed|rejected>
- **Context**: …
- **Alternatives**: … (rejected: why)
- **Decision**: …
- **Consequence**: …
```

Update the "Applicability today" table if the new ADR supersedes or narrows an earlier one.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `GLOSSARY.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-103 (WebSocket with REST fallback), but worth reopening because…_
