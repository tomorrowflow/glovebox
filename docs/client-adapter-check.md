# Client-adapter check

MCP clients only SHOULD validate tool results, and an adapter may serialize structured content into the model's context, so the guarantee of ADR 0001 has to be verified at the client too (non-claim d, ADR 0006). This check takes a capture of what a client handed the model for gate tool calls and fails when anything but permitted fields and static tool text is in it.

**The result holds for the one client and version the capture names, and for nothing else. Every further target client, and every new version of this one, needs its own check.** A pass says nothing about another client, a client's other settings, or the agent's other tools and data sources (ADR 0001, non-claim c).

## Target client

The target is OpenClaw: the client the operator's agents run on (ADR 0007 names the OpenClaw `main` agent as the consumer of the gate). The check is run against the main Gateway, not against a node-side `openclaw`, which is not the client the agents run on.

| Item | Value |
|------|-------|
| Client | OpenClaw, main Gateway |
| Version | Not yet recorded. The operator notes it on the ticket card when running the procedure below. |
| How it presents tool results to the model | Not yet recorded. The operator describes it in the capture's `client.presentation` from what the recorded model request shows: the tool definitions as listed, whether the structured content, a content-block text copy or both are handed over, and any framing text the client adds. |
| Last result | None yet. The operator records the result, the date and the version on the card. |

Until the row is filled in, no claim is made about OpenClaw. Whether the gate must provide a serialized text copy of the structured content (PRD, "Permitted fields") depends on that capture: if the client hands the model nothing of a result without one, the check reports `no gate payload reached the model`, and that decides whether the gate must provide the copy. This ticket leaves that open: such a copy would be restricted to the same validated permitted fields and covered by the acceptance tests.

## Capture procedure (operator, Gateway host)

1. On the Gateway host, add a temporary `mcp.servers` entry for the test gate (port 8765) with a test token. Use the fictional-data test token; never a production token.
2. Run one `openclaw agent` turn without `--deliver` that lists the gate's tools and makes one successful call and one failing call (for example `list_topics`, and a call with an argument the gate refuses).
3. Record the model request the client sent for that turn, and note the OpenClaw version.
4. Write the capture file (format below) from the recorded model request: copy what the client handed the model, unchanged. Do not tidy it, and do not leave the token or any header in it.
5. Run the check (next section) and record its output, the version and the date on the card.
6. Remove the temporary `mcp.servers` entry.

## Rerun command

```
.venv/bin/python scripts/client_adapter_check.py CAPTURE.json
```

Run it from the repository root in the dev install (`python3 -m venv .venv && .venv/bin/pip install -c constraints.txt -e ".[dev]"`): it reads the corpus from `tests/hostile.py`, the fixture cast from `fixtures/cast.json` and the output schemas and tool text from `src/glovebox/`, so the check follows them. It makes no request and needs no token.

Exit status: `0` when the three captures pass, `1` when any leaks or carries more than is permitted, `2` when the capture file cannot be used (the reason is printed on stderr). Output is fixed words and counts, never the text found:

```
tool_listing: ok
success_result: ok
error_result: ok
check: ok
```

A capture that fails prints `<section>: leak (N matches)` and/or `<section>: not permitted (N findings)`, then `check: failed`. A result section that holds no gate payload at all prints `ok, no gate payload reached the model` and still exits `0`: nothing leaked, but the client may be unable to use the tool (see above), so read the lines, not only the exit status, before recording a result.

## Capture file format

One JSON object:

| Field | Meaning |
|-------|---------|
| `client` | `name`, `version` and `presentation`, each a non-empty string: which client this is, and how it presents tool results to the model. Not scanned: it is about the capture, not model input. |
| `static_text` | Optional list of strings. Text the client itself adds around gate content, exactly as it appears: a framing sentence, or the keys and values of a content-block envelope such as `type` and `text` for `{"type": "text", "text": ...}`. A declared string is accepted as static text but is still scanned for corpus strings and fragments. The operator declares it, so review the list with the capture: it is the one place where the check takes a string on trust. |
| `tool_listing` | The model input for the tool listing, as any JSON value or string. |
| `success_result` | The model input for a successful tool result. |
| `error_result` | The model input for an error result. |

Each of the three is what the model receives, copied unchanged, in whatever shape the client uses: a JSON value, a list of content blocks, a string. JSON that a client has serialized into a string is read as JSON, to a nesting depth of five.

What the check does with them:

- **Leaks.** Every string of the capture, as it is and JSON-escaped, is searched for every 12-character window (case-insensitive) of each string of the hostile corpus (`CORPUS` in `tests/hostile.py`) and of the fixture cast (`fixtures/cast.json`: people, and every string of the source services except the inference stand-ins; timestamps are permitted fields and are skipped). A cast value shorter than 12 characters, such as an upstream ID or a phone number, counts only as a whole word.
- **Tool listing.** Every string and every object key must be one of the gate's own tool definitions (`SPECS` in `src/glovebox/tool.py`: names, descriptions, schemas and their keys, as the MCP wire form spells them, such as `inputSchema`) or declared `static_text`. If the client renames a key or rewrites a value, it fails until declared static, which makes the rewrite visible in the capture. A listing with no text at all is no capture and is refused with exit status 2. Numbers and booleans are not checked: they carry no text.
- **Results.** Every JSON object with an `error` field is a gate payload and must be accepted exactly (no coercion) by one of the gate's output models (`src/glovebox/output.py`), which are closed: any extra field or free string fails. Every other string and object key must be declared `static_text`. The success capture may hold only `error: "none"` payloads and the error capture only others; a capture that swaps them is refused with exit status 2.

## Fixture captures

`tests/captures/` holds the captures `tests/test_client_adapter_check.py` runs the check on. They describe a made-up client, not OpenClaw:

| File | Shows |
|------|-------|
| `clean.json` | A pass: `list_topics` as listed, a success payload and an error payload. |
| `leak-tool-listing.json` | A corpus string appended to a tool description. |
| `leak-success-result.json` | A corpus string sent as a second text block of a success result. |
| `leak-error-result.json` | A corpus string sent as a second text block of an error result. |

The tests also show that every corpus string is caught in each of the three sections, that a fragment is caught, that corpus text inside serialized JSON is caught, that an extra field, a free string, unlisted client text and altered tool text fail, and that the gate's whole tool listing passes.
