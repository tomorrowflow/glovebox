# Research 3: Attacker-writable fields and read-only credentials

Source: Fróði, answered on the card on 2026-11-08. Confidence: high on schemas and scopes; medium on deployment, and on Signal (protocol source only; no official read-only API found).

## Mail

The sender controls subject, From display name and address, Reply-To, recipient headers, Date, Message-ID and References, body, MIME declarations and attachment filenames.

- RFC address, date and message-id grammar is not authenticity or injection safety; quoted local-parts can contain words.
- MIME types allow extension tokens; parameters are strings.
- IMAP UID and UIDVALIDITY are mailbox-server identifiers, not the RFC Message-ID; Gmail and Graph resource IDs likewise differ from sender headers.
- Normalize timestamps; never expose raw header strings.

Sources: https://www.rfc-editor.org/rfc/rfc5322#section-3.6 , https://www.rfc-editor.org/rfc/rfc2045#section-5.1 , https://www.rfc-editor.org/rfc/rfc2183#section-2.3 , https://www.rfc-editor.org/rfc/rfc9051

## Calendar

The organizer or invite sender controls summary/title, description, location, UID, URLs, attachment names and URIs, attendee addresses and names, and initial times and recurrence. Attendees may submit responses and comments, subject to permissions.

- iCalendar TEXT is free text; CAL-ADDRESS is URI syntax, not verified identity.
- DATE/DATE-TIME, booleans and participation status have defined syntax, but PARTSTAT permits extension tokens.
- Google `responseStatus` is a documented enum; attendee comment, `displayName` and `extendedProperties` are strings.
- Google event IDs can be supplied at creation; `iCalUID` is not trusted text.

Sources: https://www.rfc-editor.org/rfc/rfc5545 , https://developers.google.com/workspace/calendar/api/v3/reference/events

## Messengers

Bodies, captions, names, quotes, previews, filenames and MIME declarations are tainted. Room and group names and topics need membership or admin authority but remain remote-authored.

- Matrix: the reaction key is a string, not an emoji-only enum; event content and type are extensible; IDs are opaque; origin timestamps come from federation servers.
- Signal: the protobuf includes string emoji, fileName, contentType and preview title, and client timestamps.
- Telegram: sender-defined filename and MIME, names and usernames, group titles, captions and reactions; `message_id` is an integer scoped to a chat, `date` is numeric, the reaction type is discriminated (emoji, custom emoji, paid).
- WhatsApp: the webhook includes `profile.name`, text, media metadata, contacts, location and reactions; IDs are opaque, timestamps are numeric strings.
- Remap upstream IDs to local handles.

Sources: https://spec.matrix.org/latest/client-server-api/ , https://raw.githubusercontent.com/signalapp/Signal-Android/main/lib/libsignal-service/src/main/protowire/SignalService.proto , https://core.telegram.org/bots/api , https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/components

## Read-only credentials (comparison, not an MVP selection)

- Gmail `gmail.metadata` excludes bodies but still returns hostile headers; `gmail.readonly` permits body classification.
- Graph delegated `Mail.ReadBasic` excludes body and attachments; `Mail.Read` includes them.
- Google `calendar.events.readonly`; a freebusy scope covers availability only.
- CalDAV can use a server-ACL read-only principal.
- IMAP EXAMINE/BODY.PEEK avoids writes but is not a credential boundary.
- Telegram: a bot token permits sends. Matrix: the standard full-access scope permits writes. Signal: a linked-device integration has no verified read-only scope. WhatsApp: the messaging permission is not receive-only.
- Mail and calendar therefore have clearer read-only credentials than messengers.
- A gateway allowlist alone does not reduce upstream credential power.

Sources: https://developers.google.com/workspace/gmail/api/auth/scopes , https://learn.microsoft.com/en-us/graph/permissions-reference , https://developers.google.com/workspace/calendar/api/auth , https://www.rfc-editor.org/rfc/rfc4791#section-6 , https://developers.facebook.com/docs/permissions

## Boundary implication

Expose fixed local enums and booleans, bounded parsed numbers and times, and local opaque references. Do not expose raw addresses, names, IDs, MIME or RSVP extension strings, or error echoes. Schema typing alone cannot prove the tagline.
