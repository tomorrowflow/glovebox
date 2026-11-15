---
status: accepted
---

# One Python service, the official MCP SDK, SQLite

The gate is one Python service built on the official MCP Python SDK, with SQLite for handles, classifications and sync state, and existing maintained connector libraries or bridges for the sources. Agents connect over authenticated Streamable HTTP. Operator configuration (accounts, topics, agent tokens, inference) stays outside MCP.

We chose the smallest stack that can hold the boundary in one place: one process, one output-validation path, one database file. No microservice framework, no message broker and no custom model-serving stack. Device-dependent bridges (for example iMessage) may run as separate processes where the source requires it.

## Consequences

- A bridge that runs separately is still inside the trust boundary of its source: it may hold raw text and a write-capable session, and must be unreachable for agents (ADR 0004).
- Local inference uses an existing runtime, not one built for this project.
