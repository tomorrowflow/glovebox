# Research 1: MCP tool output and structured content

Source: Fróði, answered on the card on 2026-11-08. Confidence: high on the protocol; client and model presentation is implementation-dependent. At fetch time `/specification/latest/server/tools` redirected to the **2026-07-28** revision.

## Findings

1. **A tool can return structured data with no free-text block.** Declare `outputSchema`, return `structuredContent` and `content: []` (with the current `resultType: "complete"`). `content` is a required array but need not contain a TextContent; omitting the field is different. Structured content can now be any JSON value. The Tools "Structured Content" section says a server **SHOULD**, not MUST, also return serialized JSON in a TextContent block for backwards compatibility. Omitting that copy needs a considered compatibility exception; it is not a wire-format violation.
2. **No universal text-block or model-visibility guarantee.** MCP transports the result to a client, not directly into a model. A client receives whatever blocks the server sent; no protocol rule inserts a text block. Adapters may serialize structured data for model APIs or ignore it. MCP does not specify one client-to-model representation, so empty `content` is not a promise that the model never sees serialized JSON. Target clients and adapters need verification.
3. **Strings are data, not a security compartment.** `type: "string"` accepts instruction-bearing prose, and JSON escaping or schema conformance does not neutralize prompt injection once the value reaches a model. Fixed `enum`/`const`, closed objects (`additionalProperties: false`), bounded server-generated opaque IDs and output validation can constrain the payload. A schema alone does not establish provenance or opacity. `outputSchema` covers **structuredContent** only, not parallel `content`, errors, metadata, resource dereferences or tool descriptions. Servers MUST conform; clients only SHOULD validate.
4. **Security.** Tools, Security Considerations: servers must sanitize outputs and clients SHOULD validate results before passing them to LLMs. The specification (Implementation Guidelines) says MCP cannot enforce its security principles at protocol level. OpenAI's agent safety guidance describes injection through untrusted text, recommends enums and fixed schemas to constrain flows, and says structured outputs and isolation reduce the risk without removing it. MCP `structuredContent` is unrelated to schema-constrained LLM generation.

## Implication

MCP supports an enum-and-reference contract but does not enforce an injection-proof boundary. Server code must keep attacker text out of every exposed channel (structured content, text content, errors, metadata, tool descriptions, resources), and host or adapter behaviour must preserve that boundary. A serialized copy of only safe enums and generated references adds no original message prose; unconstrained strings do.

## Sources

- https://modelcontextprotocol.io/specification/2026-07-28/server/tools#structured-content
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools#output-schema
- https://modelcontextprotocol.io/specification/2026-07-28/schema#calltoolresult
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools#security-considerations
- https://modelcontextprotocol.io/specification/2026-07-28#implementation-guidelines
- https://platform.openai.com/docs/guides/agent-builder-safety
