---
status: accepted
---

# Semantic fields come from a quarantined classifier inside the gate

Mail triage needs judgments that deterministic parsing cannot make: topic, reply-needed, action-requested and deadline. A quarantined classifier inside the gate reads the attacker-controlled text, has no tools and no upstream credentials, and proposes only those semantic fields. Server code validates every result against the closed vocabulary; anything invalid becomes `unknown`, never raw text.

- Topic is an operator-configured topic ID (or `other` / `unknown`), never a name and never an agent- or classifier-created category.
- Reply-needed and action-requested are `yes` / `no` / `unknown`.
- Deadline is a valid timestamp within the configured date range, or `unknown`.
- No summaries, quotations, generated topic names or explanations cross the gate.
- Semantic fields are untrusted judgments and never authorize an action. Injection can steer the classifier within the allowed vocabulary (ADR 0001, non-claim a).
- Classifications are cached until the item's content or the operator configuration changes.

Inference is local by default. External inference requires explicit per-account operator configuration, especially for work mail, and there is no automatic cloud fallback. Classifier inputs and raw classifier outputs are excluded from logs.

## Considered Options

- **Deterministic parsing only.** Rejected: too limited for the semantic tasks in scope.
- **Arbitrary "does this match X?" queries from the agent.** Rejected for the MVP: each query is another bit an attacker can steer and another side channel.
- **Let the agent's own model read the text behind a warning.** Rejected: that is exactly the path the gate exists to close.

## Consequences

- Local inference has resource costs on the gateway host; the spec acknowledges them without prescribing hardware.
- Classification quality is a separate concern from text isolation and is tested separately (ADR 0006).
