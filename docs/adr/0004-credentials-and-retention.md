---
status: accepted
---

# Plain permission-restricted credential files, agent tokens, and no raw-text archive

**Credentials.** Upstream credentials live in permission-restricted files outside agent access, without application-level encryption or key management. This is the operator's explicit decision: the gateway host is trusted, and encryption at rest with its own key handling would be overengineering here. Do not add it back without a new decision.

**Agent access.** Agents never receive upstream credentials. Each agent gets a revocable agent token scoped to specific accounts and read operations. Pairing and account setup are operator-only and stay outside MCP.

**Retention.** Raw text is fetched for processing and discarded. The gate persists only permitted fields, handle mappings and sync state, and re-fetches from the source when an item must be reclassified. There is no raw-message archive in Glovebox. Messenger bridges that a source requires may retain their own local history; that is documented per source and kept inaccessible to agents.

## Considered Options

- **Application-level credential encryption or a secrets manager.** Rejected by the operator as overengineering for a single trusted host.
- **A local raw-message store for fast reclassification.** Rejected: it would create a new copy of exactly the text the gate keeps away from agents.

## Consequences

- Anyone who can read files as the gateway's Linux user can read the upstream credentials; host and user separation is the protection.
- Reclassification after a topic-list change costs a re-fetch, and items that have left the source or the retrieval window cannot be reclassified.
