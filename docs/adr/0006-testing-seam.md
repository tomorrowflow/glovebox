---
status: accepted
---

# One acceptance seam: the MCP tool-call boundary

Acceptance tests drive the gate through MCP tool calls, as an agent would, with deterministic source fixtures and a stub classifier behind it. This is the only seam, because it is the boundary the guarantee is stated at: a test that passes below it proves nothing about what an agent receives.

At this seam the tests cover: a hostile-string corpus placed in every attacker-writable upstream field, malformed classifier output, sanitized errors, account isolation between agent tokens, pagination and output limits, and the absence of write tools.

One small client-adapter check sits beside it and verifies what actually reaches model context for a target client, because MCP clients only SHOULD validate and an adapter may serialize structured content into the model's context (ADR 0001, non-claim d).

Text isolation is tested separately from classification quality. Neither schema validation nor an adversarial corpus proves blanket immunity; the tests demonstrate the contract of ADR 0001, they do not extend it.

## Consequences

- Sources and the classifier need fixture and stub implementations from the first slice, which the environments also use as stand-ins (ADR 0007).
- Each target client needs its own adapter check; passing for one client says nothing about another.
- Checks against live accounts, paired devices or a real provider outage cannot run on a factory node and are marked `Operator-run:` in the spec and tickets.
