"""The Office 365 calendar source: Microsoft Graph with a credential that cannot write.

The gate signs in to the work account as a delegated user with the one permission
`Calendars.Read` and reads the default calendar through `calendarView`, which Graph expands
into single occurrences. Nothing is requested that could create, change or answer an event,
so read-only is enforced by the provider (ADR 0003). Sign-in uses Microsoft's own library,
`msal`; the operator signs in once, from the gateway host, with `glovebox o365 login`.

The credentials file is a JSON object: `client_id`, `tenant_id`, `timezone` (an IANA name, UTC
if left out), an `authority` that replaces the one built from the tenant (a test tier's
stand-in, a national cloud), and the `token_cache` that `msal` keeps, with the refresh token in
it. The gate writes the cache back when `msal` refreshes it (ADR 0004).

As for every calendar source, titles, locations, attendees, organizers, bodies and every other
upstream value are never read out of a response (ADR 0004), and nothing an upstream said
reaches an exception, a log line or a response: a failure is a fixed class only.
"""

import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TextIO
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import msal

from glovebox.calendars import (
    CalendarSource,
    SourceCalendar,
    SourceEvent,
    clip_to_look_ahead,
    combined_status,
    identified,
    identity,
    look_ahead,
    midnight,
    minute,
)
from glovebox.credentials import CredentialsError, Secret, read_credentials
from glovebox.fields import BusyStatus
from glovebox.sources import (
    FETCH_TIMEOUT_SECONDS,
    DocumentReader,
    SourceBlocked,
    SourceError,
    SourceKind,
)

# msal reports why a request failed in text that may quote what the upstream said.
logging.getLogger("msal").setLevel(logging.CRITICAL + 1)

# The only permission the gate asks for: read access to the signed-in user's calendars. The
# refresh token (`offline_access`) is added by msal and carries no access to any data.
READ_SCOPES = ["Calendars.Read"]
DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
PAGE_SIZE = 100
MAX_PAGES = 100
EVENT_FIELDS = "id,start,end,isAllDay,isCancelled,showAs,responseStatus"

# Token errors that mean access is not granted (consent missing or withdrawn, the sign-in
# revoked or expired), as opposed to the service being down.
BLOCKING_ERRORS = frozenset(
    {
        "access_denied",
        "consent_required",
        "interaction_required",
        "invalid_client",
        "invalid_grant",
        "invalid_scope",
        "login_required",
        "unauthorized_client",
    }
)
# A scope that could change a calendar. A token that holds one is refused, whatever the gate
# asked for, so that a credential able to write is never used.
WRITE_SCOPE = re.compile(r"ReadWrite|Write|Send|Manage|FullControl|Delete|Create", re.IGNORECASE)

# What Graph says about an event, as a busy status. A value that is not a key here is outside
# the vocabulary, Graph's own `unknown` included: it says nothing about the time.
SHOW_AS: dict[str, BusyStatus] = {
    "free": "free",
    "tentative": "tentative",
    "busy": "busy",
    "oof": "busy",
    "workingElsewhere": "free",
}
RESPONSES: dict[str, BusyStatus] = {
    "none": "busy",
    "organizer": "busy",
    "accepted": "busy",
    "tentativelyAccepted": "tentative",
    "notResponded": "tentative",
    "declined": "free",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Login:
    """What the credentials file holds, checked."""

    client_id: str
    authority: str
    custom_authority: bool
    timezone: ZoneInfo
    token_cache: str  # as msal serializes it
    document: Mapping[str, object]  # the whole file, to write it back with a fresh cache


def source_problem(location: str) -> str | None:
    """Why this is not the Graph address of an account, in fixed text, or None.

    The access token goes to this address, so it is only ever an https URL that holds no
    credential.
    """
    parts = urlsplit(location)
    if parts.scheme != "https" or not parts.hostname:
        return "the Office 365 source is the https address of Microsoft Graph"
    return None


def read_login(path: Path) -> Login:
    """The sign-in the credentials file holds. Raises CredentialsError, with fixed text, if
    the file cannot be used."""
    try:
        document = json.loads(read_credentials(path).reveal())
    except ValueError:
        raise CredentialsError("the credentials file is not an Office 365 login") from None
    return login_from(document)


def login_from(document: object) -> Login:
    """The sign-in a credentials document holds. Raises CredentialsError, with fixed text, if
    it is not one."""
    if not isinstance(document, dict):
        raise CredentialsError("the credentials file is not an Office 365 login")
    client_id, tenant = document.get("client_id"), document.get("tenant_id")
    authority, timezone = document.get("authority"), document.get("timezone", "UTC")
    cache = document.get("token_cache", {})
    if not isinstance(client_id, str) or not client_id or not isinstance(cache, dict):
        raise CredentialsError("the credentials file is not an Office 365 login")
    if authority is None:
        if not isinstance(tenant, str) or not tenant or "/" in tenant:
            raise CredentialsError("the credentials file names no tenant")
        authority = f"{DEFAULT_AUTHORITY}/{tenant}"
    if not isinstance(authority, str) or source_problem(authority) is not None:
        raise CredentialsError("the credentials file names no usable authority")
    try:
        zone = ZoneInfo(timezone) if isinstance(timezone, str) else None
    except (ValueError, OSError, KeyError):
        zone = None
    if zone is None:
        raise CredentialsError("the credentials file names no usable time zone")
    return Login(
        client_id=client_id,
        authority=authority,
        custom_authority=document.get("authority") is not None,
        timezone=zone,
        token_cache=json.dumps(cache),
        document=document,
    )


def write_login(
    path: Path, document: Mapping[str, object], cache: msal.SerializableTokenCache
) -> None:
    """Write the credentials file again with the cache as it is now, replacing it whole so that
    a reader never sees half of it. The new file is closed to group and others from the start."""
    content = json.dumps({**document, "token_cache": json.loads(cache.serialize())}, indent=2)
    target = os.path.realpath(path)  # a symlinked secret stays one
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".o365-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def application(login: Login, cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        login.client_id,
        authority=login.authority,
        token_cache=cache,
        # A custom authority is the operator's own: Microsoft is not asked about it.
        instance_discovery=False if login.custom_authority else None,
        timeout=FETCH_TIMEOUT_SECONDS,
    )


def grants_write(scope: object) -> bool:
    """Whether the scopes a token reports include one that could change a calendar."""
    return isinstance(scope, str) and WRITE_SCOPE.search(scope) is not None


class O365CalendarSource:
    """The default calendar of a work account, read through Microsoft Graph."""

    kind: SourceKind = "o365_calendar"

    def __init__(self, graph: str, credentials: Path) -> None:
        self._graph = graph.rstrip("/")
        self._credentials = credentials

    def fetch_events(self, now: datetime) -> SourceCalendar:
        login = read_login(self._credentials)
        token = self._access_token(login)
        window_start, window_end = look_ahead(now)
        url = self._calendar_view(window_start, window_end)
        headers = {"Prefer": f'outlook.timezone="{login.timezone.key}"'}
        events: list[SourceEvent] = []
        skipped = 0
        for _ in range(MAX_PAGES):
            page = DocumentReader(url, token, headers).read_document(self._parse_page)
            for raw in page.values:
                event = parse_event(raw, login.timezone)
                if event is None:
                    skipped += 1
                elif clip_to_look_ahead(event.start, event.end, now) is not None:
                    events.append(event)
            if page.next_link is None:
                return SourceCalendar(events=identified(events), skipped=skipped)
            url = page.next_link
        raise SourceError

    def _calendar_view(self, start: datetime, end: datetime) -> str:
        query = {
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$select": EVENT_FIELDS,
            "$top": str(PAGE_SIZE),
        }
        pairs = (
            f"{quote(key, safe='$')}={quote(value, safe=',:')}" for key, value in query.items()
        )
        return f"{self._graph}/me/calendarView?{'&'.join(pairs)}"

    def _access_token(self, login: Login) -> Secret:
        """An access token for the read permission, from the cache or by refreshing it.

        Raises SourceBlocked if the sign-in is missing, has been withdrawn, or holds a
        permission that could write, and SourceError if the service could not be asked.
        """
        cache = msal.SerializableTokenCache()
        cache.deserialize(login.token_cache)
        try:
            app = application(login, cache)
            accounts = app.get_accounts()
            # Unlike `acquire_token_silent`, this tells a refresh that failed (a dict with an
            # error) from a cache that holds no sign-in (None).
            result = (
                app.acquire_token_silent_with_error(READ_SCOPES, account=accounts[0])
                if accounts
                else None
            )
        except Exception:  # the library's failures are many; none may carry detail
            raise SourceError from None
        if result is None:
            raise SourceBlocked
        error = result.get("error")
        if error is not None:
            blocked = isinstance(error, str) and error in BLOCKING_ERRORS
            raise SourceBlocked if blocked else SourceError
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise SourceError
        if grants_write(result.get("scope")):
            raise SourceBlocked
        if cache.has_state_changed:
            try:
                write_login(self._credentials, login.document, cache)
            except OSError:
                # The old refresh token usually still works; the next sync tries again.
                logger.warning("the Office 365 credentials file could not be updated")
        return Secret(token)

    def _parse_page(self, document: object) -> "Page":
        if not isinstance(document, Mapping):
            raise SourceError
        values, link = document.get("value"), document.get("@odata.nextLink")
        if not isinstance(values, list) or not (link is None or isinstance(link, str)):
            raise SourceError
        if link is not None and not self._is_graph(link):
            # The token is sent only where it was meant to go.
            raise SourceError
        return Page(values, link)

    def _is_graph(self, url: str) -> bool:
        ours, theirs = urlsplit(self._graph), urlsplit(url)
        return (theirs.scheme, theirs.netloc) == (ours.scheme, ours.netloc)


@dataclass(frozen=True)
class Page:
    values: list[object]
    next_link: str | None


def open_o365_calendar(location: str, credentials: Path | None) -> CalendarSource:
    """The adapter for one Office 365 calendar account.

    Raises CredentialsError, which carries no detail, if the file cannot be used, and
    SourceBlocked if there is none or the source is not the https address of Graph.
    """
    if credentials is None or source_problem(location) is not None:
        raise SourceBlocked
    read_login(credentials)
    return O365CalendarSource(location, credentials)


def parse_event(raw: object, zone: ZoneInfo) -> SourceEvent | None:
    """One occurrence as the gate keeps it, or None if it cannot be understood."""
    if not isinstance(raw, Mapping):
        return None
    event_id, all_day = raw.get("id"), raw.get("isAllDay", False)
    if not isinstance(event_id, str) or not event_id or not isinstance(all_day, bool):
        return None
    start, end = _local(raw.get("start"), zone), _local(raw.get("end"), zone)
    if start is None or end is None:
        return None
    if all_day:
        # Dates, `end` exclusive, in the calendar's time zone.
        first, last = start.date(), end.date()
        if last <= first:
            return None
        begin, finish = midnight(first, zone), midnight(last, zone)
    else:
        begin, finish = minute(start.astimezone(UTC)), minute(end.astimezone(UTC))
        if finish <= begin:
            return None
    return SourceEvent(
        upstream_id=identity(event_id, None),
        start=begin,
        end=finish,
        all_day=all_day,
        busy_status=busy_status(raw),
    )


def _local(value: object, zone: ZoneInfo) -> datetime | None:
    """A Graph `dateTimeTimeZone` as an aware time. Graph answers in the zone it was asked for
    (or UTC); a time in any other zone is not understood."""
    if not isinstance(value, Mapping):
        return None
    moment, name = value.get("dateTime"), value.get("timeZone")
    if not isinstance(moment, str) or not isinstance(name, str):
        return None
    wall: tzinfo
    if name == "UTC":
        wall = UTC
    elif name == zone.key:
        wall = zone
    else:
        return None
    try:
        parsed = datetime.fromisoformat(moment)
    except ValueError:
        return None
    return None if parsed.tzinfo is not None else parsed.replace(tzinfo=wall)


def busy_status(raw: Mapping[str, object]) -> BusyStatus:
    """The busy status of an event from whether it is cancelled, how its time is shown and how
    the operator answered it; anything outside what Graph documents makes it `unknown`."""
    cancelled = raw.get("isCancelled", False)
    shown, answer = raw.get("showAs"), raw.get("responseStatus")
    response = answer.get("response") if isinstance(answer, Mapping) else None
    return combined_status(
        [
            ("free" if cancelled else "busy") if isinstance(cancelled, bool) else None,
            SHOW_AS.get(shown) if isinstance(shown, str) else None,
            RESPONSES.get(response) if isinstance(response, str) else None,
        ]
    )


class LoginError(Exception):
    """The sign-in did not complete. The message is fixed text."""


def sign_in(
    credentials: Path,
    *,
    client_id: str,
    tenant_id: str,
    timezone: str,
    authority: str | None,
    out: TextIO,
) -> None:
    """Sign the operator in to the work account with the device code flow, asking for the read
    permission only, and write the credentials file.

    Raises LoginError if the sign-in fails or the token that comes back could write.
    """
    document = {"client_id": client_id, "tenant_id": tenant_id, "timezone": timezone}
    if authority is not None:
        document["authority"] = authority
    cache = msal.SerializableTokenCache()
    try:
        app = application(login_from(document), cache)
        flow = app.initiate_device_flow(scopes=READ_SCOPES)
        if "user_code" not in flow:
            raise LoginError("the sign-in could not be started")
        print(flow["message"], file=out, flush=True)
        result = app.acquire_token_by_device_flow(flow)
    except CredentialsError as error:
        raise LoginError(str(error)) from None
    except LoginError:
        raise
    except Exception:  # the library's failures are many; none may carry detail
        raise LoginError("the sign-in failed") from None
    if "access_token" not in result:
        raise LoginError("the sign-in was not completed")
    if grants_write(result.get("scope")):
        raise LoginError("the sign-in grants a permission that could write: it was not saved")
    write_login(credentials, document, cache)
