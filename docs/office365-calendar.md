# Source: Office 365 calendar

The `o365_calendar` source serves the operator's work calendar as busy and free windows, overlaps and upcoming event times, read-only, over the next 90 days (ADR 0003, ticket 17). It is implemented and tested against a scripted TLS stand-in of the sign-in service and Microsoft Graph (`tests/o365_standin.py`). **It has not been run against a real tenant: that is the operator's acceptance run below, and the source is delivered only if that run passes** (ADR 0003: a stand-in never counts as delivering a source). It may also never run: if the employer does not grant consent, the operator records the source as `blocked` and the ticket closes as blocked.

## The access method, and why

The gate signs in to the work account as the operator, once, with the OAuth device code flow, and then reads the default calendar with Microsoft Graph's `calendarView`:

```
GET https://graph.microsoft.com/v1.0/me/calendarView?startDateTime=…&endDateTime=…&$select=…
Prefer: outlook.timezone="UTC"
```

It is read twice, with a different `Prefer` header. **Timed events** are read from the view in `UTC`: a local wall time is ambiguous in the hour that repeats when summer time ends (in Berlin, 02:00 to 03:00 on the last Sunday of October happens twice), so the gate takes the UTC instant Graph gives and never guesses between the two. **All-day events** are read from a second view with `Prefer: outlook.timezone="<the calendar's IANA zone>"`, because an all-day event is a date and Graph gives its midnights in the zone it is asked for. With a calendar in `UTC` one view serves both. Each view's events of the other kind are ignored.

Graph expands recurring series into their occurrences and applies exceptions and cancellations itself, so the gate stores single occurrences and does no recurrence arithmetic for this source. Only `id`, `start`, `end`, `isAllDay`, `isCancelled`, `showAs` and `responseStatus` are selected; subjects, bodies, locations, attendees, organizers and links are never requested, and an event ID is read only for its handle.

| Option | Verdict |
|---|---|
| **`msal` plus the official Graph REST endpoint** (chosen) | `msal` is Microsoft's own Python authentication library (1.39.0, pinned). It does the device code flow, keeps the refresh token in a serializable cache and refreshes silently. The one call the gate needs, a read of `calendarView`, is a plain HTTPS GET with a bearer token, made with the same standard-library reader as every other source. |
| `msgraph-sdk` | Microsoft's official, maintained Graph SDK (1.65.0). It is asynchronous and generated around Kiota, with a long tail of dependencies, to make one GET. It would still need an authentication library under it. Not adopted; revisit when the Office 365 mail source (ticket 16, not merged when this was written) needs many Graph calls, and move both onto it together if so. |
| Application permissions (client credentials) | Rejected. An app-only `Calendars.Read` reads every mailbox in the tenant unless an administrator adds an Exchange application access policy, which is a far larger grant than one operator's calendar. The delegated permission below reaches only the signed-in user's own data. |
| ICS publish link, EWS, IMAP-style access | Not adopted. A publish link is a secret URL with no consent trail; EWS is the legacy interface Microsoft is retiring in Exchange Online. |

## Read-only enforcement: provider-enforced

The account reports `read_only_enforcement: provider`. The reason is the permission the credential holds:

| Permission | Type | What it allows |
|---|---|---|
| `Calendars.Read` | Delegated (Microsoft Graph) | Read the signed-in user's calendars and events. **No create, change, delete, accept or decline.** |
| `offline_access`, `openid`, `profile` | Delegated | The refresh token and the identity of the signed-in user; no access to data. `msal` adds these itself. |

A token that carries only these cannot write: Graph refuses a write with `403`, whatever the gate does. The gate asks for `Calendars.Read` and nothing else (`READ_SCOPES` in `src/glovebox/o365.py`), and sends nothing but `GET` requests to Graph. As a second check, a token whose reported scopes include anything that could change a calendar (`ReadWrite`, `Write`, `Send`, `Manage`, `FullControl`, `Delete`, `Create`) is refused: `glovebox o365 login` does not save it, and the gate reports the account `blocked` instead of using it.

What this does not cover: the gate's host holds a refresh token that can read this calendar until it is revoked, and the file it lives in is protected only by its mode and the host (ADR 0004). Revoke it in the Microsoft account's app permissions, or by removing the app's consent in Entra, if the host is lost.

## Setup

### 1. What the employer has to grant

An application registration in the employer's Microsoft Entra tenant, and consent for it to read calendars. Ask the tenant's administrator (or whoever runs Entra) for this, in these words:

> Register a single-tenant app, "Glovebox calendar reader", with **Allow public client flows** set to **Yes** and no redirect URI. Give it the one **delegated** Microsoft Graph permission **Calendars.Read**. No application permissions, no write permissions, no `.All` permissions. Grant admin consent for it if user consent is off in the tenant, or tell me to consent myself if it is on. Send me the Application (client) ID and the Directory (tenant) ID.

Three outcomes are possible, and only the first delivers the source:

- **Consent granted.** Continue below.
- **Consent refused or never answered.** The source stays `blocked`. Record it as `blocked` with the reason on the ticket; it closes as blocked, not delivered. Never work around the refusal: no personal app registration, no borrowed client ID, no scraping the web client, no forwarding rules (ADR 0003: workplace restrictions are never bypassed).
- **Consent granted, but the tenant blocks the device code flow** (a Conditional Access policy can). The sign-in fails and the source is `blocked` for the same reason. Ask the administrator for an exception for this one app, or record it as blocked.

Withdrawing consent later has the same effect on a running gate: the next refresh fails, the account becomes `blocked`, and the other accounts are unaffected. The gate uses an access token from its cache for as long as Microsoft says it lives (about an hour, and sooner if Microsoft revokes it), so the account turns `blocked` at the first refresh after that, not at the moment of withdrawal.

### 2. Sign in, on the gateway host, as the gateway's user

```
.venv/bin/glovebox o365 login \
  --credentials ~/glovebox-secrets/work-o365.json \
  --client-id <Application (client) ID> \
  --tenant-id <Directory (tenant) ID> \
  --timezone Europe/Berlin
```

The command prints a short code and an address. Open the address in a browser signed in as the work account, enter the code and approve. It then writes the credentials file, mode `0600`, in a directory only the gateway's user may use. Run it again to sign in anew, for example after consent was withdrawn and granted again. If the sign-in would grant a write permission, nothing is saved and the command says so.

`--timezone` is the calendar's IANA time zone (default `UTC`). It tells Graph which zone to answer in for all-day events (timed events are always read in UTC), and it is where an all-day event's midnights are: an all-day event spans local midnights in that zone, so it is 23 or 25 hours long on a daylight-saving change. Use the zone the work calendar is set to in Outlook.

### 3. Add the account and a token

```
.venv/bin/glovebox account add work --kind o365_calendar \
  --source https://graph.microsoft.com/v1.0 \
  --credentials ~/glovebox-secrets/work-o365.json
.venv/bin/glovebox token issue --account work --label work-calendar
```

`--source` is the https address of Graph (national clouds have their own); the gate refuses any other source for this kind, and refuses a credentials file that holds no sign-in or an unusable time zone, or that is open to group or others. Include the private calendar's account in `token issue` to give an agent both calendars in one call.

The credentials file is a JSON object: `client_id`, `tenant_id`, `timezone`, the `token_cache` that `msal` keeps (the refresh token is in it) and, only for a national cloud, an `authority` that replaces `https://login.microsoftonline.com/<tenant>`. The gate rewrites the file when a refresh rotates the token cache, atomically and keeping mode `0600`; the gateway's user therefore needs write access to its directory. If the write fails the sync still succeeds and a fixed warning is logged.

## What is served

- The default calendar of the signed-in user. Other calendars, shared calendars and delegated mailboxes are not read.
- Events overlapping the 90-day look-ahead from the gate's clock, one row per occurrence, up to 100 per page and 100 pages per view (a calendar not in UTC is read through two views), then the account is `stale` (the response is not a calendar the gate can vouch for). The store keeps at most 2000 events per account; the time after the first dropped event is `unknown`, never `free` (see `AGENTS.md`).
- `busy_status` combines three things Graph says. `showAs`: `busy` and `oof` are `busy`, `tentative` is `tentative`, `free` and `workingElsewhere` are `free`, and `unknown` (or anything else) makes the status `unknown`. The operator's answer: `none`, `organizer` and `accepted` are `busy`, `tentativelyAccepted` and `notResponded` are `tentative`, `declined` is `free`. `isCancelled` makes the event `free`. If any one of them is free, the event is `free`; if any is outside this vocabulary, it is `unknown`; otherwise `tentative` if any says so, else `busy`.
- A timed event whose times are not in UTC (so, if Graph ignored the request, one that could be either instant in a repeated hour), an all-day event in neither UTC nor the calendar's zone, an unreadable time, an end that is not after the start, a non-boolean all-day flag and a non-object entry are skipped and counted: the account is `incomplete`. An event that crosses the end of summer time, with an end that is earlier on the clock than its start, is kept: the gate works in instants.

## Failures

Each is a fixed class; nothing an upstream said reaches a response, a log or an exception.

| What happens | Account status |
|---|---|
| No sign-in in the credentials file; the credentials file cannot be used | `blocked` |
| The refresh is refused with `invalid_grant`, `consent_required`, `interaction_required`, `login_required`, `invalid_client`, `unauthorized_client`, `invalid_scope` or `access_denied` (consent missing or withdrawn, sign-in expired or revoked) | `blocked` |
| Graph answers `401` or `403` | `blocked` |
| The token reports a scope that could write | `blocked` |
| Sign-in service or Graph unreachable, `429` (throttling), `5xx`, a timeout, an answer that is not JSON or not the shape of a `calendarView` page, a next-page link to any other host, more than 100 pages | `stale`, with the last successful sync time kept |
| Individual events that cannot be understood | `incomplete`, with a capped count |

A `stale`, `blocked` or `incomplete` account is never answered as an empty calendar: the calendar tools return the fixed outage error with each account's status, and the account is `current` again at the next successful sync, with no operator action. A next-page link is followed only if it is on the same host as the source, so the access token is never sent elsewhere.

TLS: both `msal` (through `requests`, with `certifi`) and the Graph reader verify certificates. A corporate TLS-inspecting proxy needs its root certificate in both the system store and `REQUESTS_CA_BUNDLE`. The tests set `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` to trust the stand-in's own certificate.

## Operator acceptance run (at merge)

Not run yet. The operator, with the employer's consent in hand:

1. Sign in and add the account as above, on the gateway host.
2. Check the granted permissions are read-only: in the Microsoft account's app permissions (`myapps.microsoft.com`, or Entra's enterprise application, **Permissions**) the app holds `Calendars.Read` and no write permission. Confirm `list_accounts` reports the account with `read_only_enforcement: provider`.
3. For the coming week, compare `get_availability` and `list_events` with the real calendar in Outlook: the busy and free windows match, recurring and all-day events are there, a declined or cancelled event is free, a tentative one is tentative. Check the time zone setting (all-day events and events near a daylight-saving change).
4. Create a new event in Outlook and confirm it shows up at the next call.
5. Record the result on the ticket. If consent was not granted, or the tenant blocks the sign-in, record the source as `blocked` with the reason; the ticket closes as blocked, not as delivered.

Unverified until then, and the things to look at if the run disagrees: that Graph accepts `UTC` and the IANA zone name in `Prefer: outlook.timezone` and answers in them (an event in any other zone is skipped and the account is `incomplete`), and that an all-day event in the zone's view has its midnights in that zone; check an all-day event and an event around the last Sunday of October, and that `Calendars.Read` returns `showAs` and `responseStatus` (the narrower `Calendars.ReadBasic` was not tried, and may leave out what the gate needs).
