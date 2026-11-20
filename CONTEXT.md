# Glovebox

A gated data layer for self-hosted AI agents. It serves mail, calendar and messenger data to agents as permitted fields only, so text an attacker wrote never reaches the agent's model.

Architecture map: docs/architecture/*.json (SeeCode specs, evidence-linked; rules in docs/architecture/README.md).

## Language

### Boundary

**Gate**:
The Glovebox service that sits between upstream sources and the agents' MCP clients; everything an agent learns about a source crosses it.
_Avoid_: Proxy, filter, firewall, sanitizer

**Text isolation**:
The property that no free-form string authored by a message sender, event organizer or chat participant reaches an agent's context. It says nothing about whether a classification is correct.
_Avoid_: Injection-proof, sanitization, immunity

**Permitted field**:
A value allowed to cross the gate: a closed-vocabulary enum, a bounded parsed number or timestamp (minute precision), a boolean, or a handle.
_Avoid_: Safe field, typed field, metadata

**Handle**:
A gate-generated opaque reference to an upstream entity (item, thread, conversation, sender, event, account). Stable per account, generated independently of content, and never dereferenced into an agent's context.
_Avoid_: ID, reference, pointer, upstream ID

**Gateway-enforced read-only**:
Read-only access that holds because the gate exposes no write operation, even though the upstream credential could write. The opposite is provider-enforced read-only, where the credential itself cannot write.
_Avoid_: Read-only mode, safe mode

### Classification

**Quarantined classifier**:
The language model inside the gate that reads attacker-controlled text, holds no tools and no upstream credentials, and proposes only semantic fields that the gate validates.
_Avoid_: Summarizer, extractor, inner agent

**Semantic field**:
A permitted field whose value is a judgment of the quarantined classifier: topic, reply-needed, action-requested, deadline. Always untrusted, never an authorization for an action.
_Avoid_: Label, tag, insight

**Topic**:
One entry of the operator-configured topic list, crossing the gate as its topic ID, or `other` / `unknown`.
_Avoid_: Category, label, folder

**Unknown**:
The value a field takes when the gate cannot produce a valid one, including when the quarantined classifier's output fails validation.
_Avoid_: Null, empty, error

### Parties

**Source**:
One kind of upstream data served through the gate: mail, calendar or a messenger at a specific provider.
_Avoid_: Integration, connector, backend

**Account**:
One of the operator's identities at a source, set up by the operator and referred to by an account handle.
_Avoid_: Mailbox, profile, tenant

**Operator**:
The person who runs the gate, configures accounts and topics, issues agent tokens and owns the upstream credentials.
_Avoid_: Admin, user, owner

**Agent**:
An AI agent that reads through the gate as an MCP client and is treated as exposed to prompt injection from everywhere else.
_Avoid_: Client, bot, assistant

**Agent token**:
A revocable credential the operator issues to one agent, scoped to specific accounts and read operations. It is not an upstream credential.
_Avoid_: API key, password, session
