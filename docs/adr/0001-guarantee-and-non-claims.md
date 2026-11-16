---
status: accepted
---

# The guarantee is text isolation, with stated non-claims

The tagline "prompt injection out by construction" is the goal, never a proven blanket claim. Research (`docs/research/01-mcp-structured-output.md`, `docs/research/02-prior-art.md`) showed that MCP does not enforce an injection-proof boundary and that published isolation designs support a narrower claim than the tagline. We therefore fix one contract, and neither weaken it silently nor claim more than it.

**The guarantee.** No free-form string authored by a message sender, event organizer or chat participant crosses the gate into the agent's context. Outputs contain only closed-vocabulary enums, bounded parsed numbers and timestamps, booleans, and gateway-generated opaque handles.

**Non-claims.**

(a) An attacker may influence classifications and values: a hostile message can yield a valid but wrong `urgent: true`. The guarantee is text isolation, not classification correctness.

(b) Enum, count, ordering and timing side channels are bounded, not zero.

(c) The guarantee covers every gate-controlled output channel (structured content, content blocks, errors, metadata, tool descriptions) but not the agent's other tools or data sources.

(d) The client/model adapter must be verified to preserve the boundary.

## Considered Options

- **Claim blanket prompt-injection immunity.** Rejected: no reviewed source establishes it, and schema typing alone cannot prove it.
- **Allow sanitized or length-limited strings (subjects, sender names).** Rejected: a string is data, not a security compartment; any attacker-authored string that reaches the model can carry instructions.

## Consequences

- Sender names, addresses, subjects, filenames, upstream IDs and upstream error text never cross the gate, which limits what an agent can tell the operator about an item. The agent refers to items by handle; showing the text is someone else's job.
- Output limits bound the side channels: at most 50 items per response with opaque pagination, fixed sorting choices with no classifier-generated ranking, counts capped at `1000+`, timestamps at minute precision, no arbitrary semantic queries.
- Every response is validated as a whole before release; a response that fails validation is replaced by a fixed error enum, never released in part.
