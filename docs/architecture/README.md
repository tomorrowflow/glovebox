# Architecture map

SeeCode JSON specs that describe the system. They are the source of truth for agents reading the architecture as context, and the viewer behind the board's **Architecture** button renders them.

| Spec | Type | Shows |
|------|------|-------|
| `overview.json` | `high-level` | The whole system for a non-engineer |
| `glovebox.json` | `architecture` | `src/glovebox`: entry point, MCP server and ASGI app |
| `classifier.json` | `architecture` | The quarantined classifier: what it receives, what is validated, what is cached |
| `deployment.json` | `deployment` | Where Glovebox runs: the throwaway Compose stack (gate, standins, seed), plus the native test and production tiers on the Mac mini |

## Rules

- Never invent components. Draw only what the code has; do not draw what an ADR or spec only plans.
- Mark anything inferred rather than seen in code with `"kind": "optional"`, and say so in the card report.
- Patch specs, do not rewrite them: `node ~/.claude/skills/seecode/scripts/seecode.mjs render docs/architecture/<spec>.json --patch '<json>'`.
- Keep evidence as `file:line`: every non-obvious node or edge has an `evidence` entry `{"id", "file", "line"}`. Edge ids are `from>to`.
- Stay within the budget: at most 9 nodes and 12 edges per spec. Split a spec rather than exceed it.
- Commit only the JSON. Rendered `*.html` is ignored by `.gitignore`.

## Render

```
node ~/.claude/skills/seecode/scripts/seecode.mjs render docs/architecture
```

Every spec must return `ok:true` with no `W_` warning. Settings are in `.seecode/config.json`.
