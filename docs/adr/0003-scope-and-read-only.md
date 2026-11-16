---
status: accepted
---

# MVP scope: read-only triage and availability over live sources

The MVP serves mail triage (read status, age, attachment presence, grouping by opaque sender and thread handles), calendar availability (busy/free windows, overlaps, upcoming event times) and the semantic fields of ADR 0002. Handles support listing, filtering, grouping, counting and sorting on permitted fields, and nothing else.

**No upstream mutation.** No sending, drafting, marking read, label changes, invite acceptance or any other change upstream, including seemingly minor mailbox changes. The gate exposes no write operation.

**Sources, all in initial scope:** mailbox.org mail and calendar, work Office 365 mail and calendar, Signal, iMessage, WhatsApp. Gmail is out. Priority is live source coverage before classification quality, without relaxing text isolation. A source is delivered when it has live ingestion, opaque handles, permitted metadata and the same semantic fields. A source blocked by access requirements (admin consent, device pairing) stays explicitly blocked and reported while the others ship; a canned integration is never substituted for live support, and workplace restrictions are never bypassed.

**Read-only enforcement.** Research (`docs/research/03-attacker-fields-and-credentials.md`) found that mail and calendar have clearer read-only credentials than messengers: IMAP EXAMINE is not a credential boundary and messenger bridges hold write-capable sessions. Where the provider cannot enforce read-only at the credential level, gateway-enforced read-only is acceptable. Each source documents whether its read-only is provider-enforced or gateway-only, and prefers provider-enforced where available.

**Retrieval window.** Rolling 30-day mail ingestion window; 90-day calendar look-ahead.

**Deferred:** attachment contents, OCR, voice transcription, historical backfill beyond the bounded recent window.

## Consequences

- A gateway-only read-only source still holds a credential that could write; the protection is that agents never receive it and the gate has no code path that writes.
- Office 365 may need employer or admin consent, and Signal, iMessage and WhatsApp depend on devices or bridges (iMessage on the Apple-device setup). Any of these can block a source indefinitely.
