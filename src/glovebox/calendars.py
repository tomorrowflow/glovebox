"""Calendar sources: the only code that sees raw upstream event structure.

An adapter fetches one account's events and hands the gate normalized ones: a start and an
end in UTC at minute precision, an all-day flag and a busy status from a closed vocabulary.
Titles, descriptions, locations, attendees, organizers, URLs, attachments, comments and every
other upstream value are never read out of the document (ADR 0004), and an event's UID is
read only for the handle registry. Unlike mail, an event is not text for the classifier:
events carry no semantic fields.

Recurring events are expanded here, into one event per occurrence inside the look-ahead, so
the gate stores and serves single occurrences only. An occurrence keeps its wall-clock time
in the event's time zone, and so moves in UTC across a daylight-saving change; an all-day
event spans local midnights, and so is 23 or 25 hours long on the day of a change.
"""

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from glovebox.credentials import read_credentials
from glovebox.fields import BusyStatus
from glovebox.formats import LOOK_AHEAD_DAYS
from glovebox.sources import (
    CALENDAR_KINDS,
    DocumentReader,
    SourceError,
    SourceKind,
)

# The statuses that hold time: busy and tentative events, and ones that might be either.
# They make busy windows and can overlap; free ones do neither.
BLOCKING_STATUSES: tuple[BusyStatus, ...] = ("busy", "tentative", "unknown")

FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
WEEKDAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")
MAX_INTERVAL = 1000
# Candidate dates examined for one recurring event; a rule that needs more is not understood.
MAX_CANDIDATES = 20_000

# How each upstream value maps to a busy status, and what an absent value means. A value that
# is not a key here, or not a string, is outside the vocabulary.
STATUS_TABLES: dict[str, tuple[str, dict[str, BusyStatus]]] = {
    "status": ("confirmed", {"confirmed": "busy", "tentative": "tentative", "cancelled": "free"}),
    "transparency": ("opaque", {"opaque": "busy", "transparent": "free"}),
    "participation": (
        "accepted",
        {
            "accepted": "busy",
            "tentative": "tentative",
            "needs_action": "tentative",
            "declined": "free",
        },
    ),
}


@dataclass(frozen=True)
class SourceEvent:
    """One occurrence of an event as the gate keeps it: the upstream key and the values that
    map to permitted fields."""

    upstream_id: str  # the UID, and for an occurrence of a recurring event the day too
    start: datetime  # UTC, truncated to the minute
    end: datetime
    all_day: bool
    busy_status: BusyStatus


@dataclass(frozen=True)
class SourceCalendar:
    events: list[SourceEvent]
    skipped: int = 0  # events that could not be understood and were left out


class CalendarSource(Protocol):
    kind: SourceKind

    def fetch_events(self, now: datetime) -> SourceCalendar:
        """Fetch the account's events inside the look-ahead that starts at `now` (UTC); a
        source may leave out the rest, and the gate drops it anyway.

        Raises SourceBlocked if access is not granted and SourceError for any other
        failure, never anything with detail.
        """
        ...


def look_ahead(now: datetime) -> tuple[datetime, datetime]:
    """The start of the look-ahead, `now` at minute precision, and its end, 90 days on."""
    start = _minute(now)
    return start, start + timedelta(days=LOOK_AHEAD_DAYS)


def clip_to_look_ahead(
    start: datetime, end: datetime, now: datetime
) -> tuple[datetime, datetime] | None:
    """The event as the look-ahead serves it, or None if it is not served at all.

    An event that has ended is not served, and nor is one that starts at or after the end of
    the look-ahead; one that runs past that end is cut there.
    """
    window_start, window_end = look_ahead(now)
    if end <= window_start or start >= window_end:
        return None
    return start, min(end, window_end)


class FixtureCalendarSource(DocumentReader):
    """Events from a fixture document.

    The document has the shape the calendar stand-ins serve: an `account`, an optional
    `timezone` (an IANA name, UTC if left out) and a list of `events`. An event needs an `id`,
    a `start` and an `end`; `all_day`, `timezone`, `status`, `transparency`, `participation`,
    `recurrence` and `exceptions` are optional, and its other keys (title, location, attendees
    and so on) are never read. Times are ISO 8601 with a UTC offset, or without one, which
    means the event's time zone. An all-day event has dates, `end` exclusive and one day after
    `start` if left out. A recurrence is a `freq` (daily, weekly, monthly or yearly) with an
    optional `interval`, `count`, `until` (a date, inclusive), and `by_day` (weekly only);
    `exceptions` are dates of occurrences to leave out. An event that cannot be understood is
    skipped and counted.
    """

    kind: SourceKind = "fixture_calendar"

    def fetch_events(self, now: datetime) -> SourceCalendar:
        return self.read_document(lambda document: parse_calendar(document, now))


def open_calendar(kind: str, location: str, credentials: Path | None) -> CalendarSource:
    """The adapter for one calendar account, once its credentials file has passed the mode check.

    Raises CredentialsError, which carries no detail, if the file cannot be used.
    """
    if credentials is not None:
        read_credentials(credentials)
    if kind not in CALENDAR_KINDS:
        raise SourceError
    is_url = urlsplit(location).scheme in ("http", "https")
    return FixtureCalendarSource(location if is_url else Path(location))


def parse_calendar(document: object, now: datetime) -> SourceCalendar:
    if not isinstance(document, Mapping):
        raise SourceError
    account = document.get("account")
    events = document.get("events")
    zone = _zone(document.get("timezone", "UTC"))
    if not isinstance(account, str) or not account or not isinstance(events, list) or zone is None:
        raise SourceError
    found: dict[str, SourceEvent] = {}
    skipped = 0
    for raw in events:
        parsed = _parse_event(raw, zone, now)
        if parsed is None:
            skipped += 1
            continue
        for event in parsed:
            distinct = _distinct(found, event)
            found.setdefault(distinct.upstream_id, distinct)
    return SourceCalendar(events=list(found.values()), skipped=skipped)


def _distinct(found: Mapping[str, SourceEvent], event: SourceEvent) -> SourceEvent:
    """The event under an identity of its own.

    A repeat of an event found already keeps that one's identity. A different event under
    the same UID gets the next free one, so that no event that holds time is dropped and
    each has a handle of its own.
    """
    candidate, number = event, 1
    while (kept := found.get(candidate.upstream_id)) is not None:
        if replace(kept, upstream_id=event.upstream_id) == event:
            break
        number += 1
        candidate = replace(event, upstream_id=json.dumps([event.upstream_id, number]))
    return candidate


def _identity(uid: str, day: date | None) -> str:
    """The key of an event or, for a recurring one, of its occurrence on `day`.

    A list of strings in JSON has one spelling and reads back one way, so a UID can never
    pass for the UID of another event plus a day.
    """
    return json.dumps([uid] if day is None else [uid, day.isoformat()])


def _parse_event(raw: object, default_zone: tzinfo, now: datetime) -> list[SourceEvent] | None:
    """The occurrences of one event that the look-ahead serves, or None if it cannot be
    understood. Date arithmetic that leaves the supported years is not understood either."""
    try:
        return _occurrences_of(raw, default_zone, now)
    except (OverflowError, ValueError):
        return None


def _occurrences_of(raw: object, default_zone: tzinfo, now: datetime) -> list[SourceEvent] | None:
    if not isinstance(raw, Mapping):
        return None
    uid = raw.get("id")
    zone = _zone(raw["timezone"]) if "timezone" in raw else default_zone
    all_day = raw.get("all_day", False)
    if not isinstance(uid, str) or not uid or zone is None or not isinstance(all_day, bool):
        return None
    rule = None
    if raw.get("recurrence") is not None:
        rule = _recurrence(raw["recurrence"])
        if rule is None:
            return None
    exceptions = _exceptions(raw.get("exceptions"))
    span = _all_day_span(raw, zone) if all_day else _timed_span(raw, zone)
    if span is None or exceptions is None:
        return None
    first_day, span_of = span
    status = busy_status_of(raw)

    events = []
    window_end = look_ahead(now)[1]
    days = [first_day] if rule is None else _days(first_day, rule)
    for day in days:
        start, end = span_of(day)
        if start >= window_end:
            break
        served = clip_to_look_ahead(start, end, now)
        if served is None or day in exceptions:
            continue
        events.append(
            SourceEvent(
                upstream_id=_identity(uid, None if rule is None else day),
                start=_minute(served[0]),
                end=_minute(served[1]),
                all_day=all_day,
                busy_status=status,
            )
        )
    return events


Span = Callable[[date], tuple[datetime, datetime]]


def _timed_span(raw: Mapping[str, object], zone: tzinfo) -> tuple[date, Span] | None:
    """The first day of a timed event, and its start and end in UTC on any day.

    The first occurrence is the instant the source gave. A later one keeps the wall-clock
    start time in the event's zone and the exact duration, as a recurring meeting does.
    """
    first_start = _instant(raw.get("start"), zone)
    first_end = _instant(raw.get("end"), zone)
    if first_start is None or first_end is None:
        return None
    if _minute(first_end) <= _minute(first_start):
        return None
    local = first_start.astimezone(zone)
    first_day, wall_time = local.date(), local.time()
    duration = first_end - first_start

    def span(day: date) -> tuple[datetime, datetime]:
        if day == first_day:
            return first_start, first_end
        start = datetime.combine(day, wall_time).replace(tzinfo=zone).astimezone(UTC)
        return start, start + duration

    return first_day, span


def _all_day_span(raw: Mapping[str, object], zone: tzinfo) -> tuple[date, Span] | None:
    """The first day of an all-day event, and the local midnights that bound it on any day."""
    first_day = _date(raw.get("start"))
    end = raw.get("end")
    if end is None:
        end_day = None if first_day is None else first_day + timedelta(days=1)
    else:
        end_day = _date(end)
    if first_day is None or end_day is None or end_day <= first_day:
        return None
    length = end_day - first_day

    def span(day: date) -> tuple[datetime, datetime]:
        return _midnight(day, zone), _midnight(day + length, zone)

    return first_day, span


def busy_status_of(raw: Mapping[str, object]) -> BusyStatus:
    """The busy status of an event from its status, transparency and participation.

    Anything outside the vocabulary makes it `unknown`; otherwise it is `free` if any of the
    three says the time is not held, `tentative` if any says it may not be, else `busy`.
    """
    mapped: list[BusyStatus | None] = []
    for key, (default, table) in STATUS_TABLES.items():
        value = raw.get(key, default)
        mapped.append(table.get(value) if isinstance(value, str) else None)
    if None in mapped:
        return "unknown"
    if "free" in mapped:
        return "free"
    return "tentative" if "tentative" in mapped else "busy"


@dataclass(frozen=True)
class Recurrence:
    freq: str
    interval: int
    count: int | None
    until: date | None
    by_day: tuple[int, ...]  # weekday numbers, Monday 0; none means the start's weekday


def _recurrence(raw: object) -> Recurrence | None:
    if not isinstance(raw, Mapping):
        return None
    freq = raw.get("freq")
    by_day = raw.get("by_day", [])
    interval = _bounded(raw.get("interval", 1), MAX_INTERVAL)
    count = None if raw.get("count") is None else _bounded(raw["count"], MAX_CANDIDATES)
    until = None if raw.get("until") is None else _date(raw["until"])
    if not isinstance(freq, str) or freq not in FREQUENCIES or interval is None:
        return None
    if (raw.get("count") is not None and count is None) or (
        raw.get("until") is not None and until is None
    ):
        return None
    if not isinstance(by_day, list) or (by_day and freq != "weekly"):
        return None
    if any(day not in WEEKDAYS for day in by_day):
        return None
    return Recurrence(
        freq=freq,
        interval=interval,
        count=count,
        until=until,
        by_day=tuple(sorted({WEEKDAYS.index(day) for day in by_day})),
    )


def _bounded(value: object, high: int) -> int | None:
    """A whole number from 1 to `high`, else None. A boolean is not a number."""
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= high:
        return value
    return None


def _exceptions(raw: object) -> set[date] | None:
    """The dates of the occurrences to leave out; None if the list cannot be understood."""
    if raw is None:
        return set()
    if not isinstance(raw, list):
        return None
    dates = [_date(value) for value in raw]
    return None if None in dates else {day for day in dates if day is not None}


def _days(first: date, rule: Recurrence) -> Iterator[date]:
    """The dates of the occurrences, from the first, as the rule gives them.

    The count and the end date apply to occurrences, left-out ones included. A rule that
    needs more than MAX_CANDIDATES dates to get anywhere is not understood.
    """
    produced = 0
    for examined, day in enumerate(_candidates(first, rule), start=1):
        if examined > MAX_CANDIDATES:
            raise OverflowError
        if day is None:
            continue
        if rule.until is not None and day > rule.until:
            return
        yield day
        produced += 1
        if rule.count is not None and produced >= rule.count:
            return


def _candidates(first: date, rule: Recurrence) -> Iterator[date | None]:
    """Each step of the rule: a date, or None where the step falls on a day that does not
    exist (the 31st in a short month, 29 February in a year that is not a leap year)."""
    step = 0
    if rule.freq == "weekly":
        week = first - timedelta(days=first.weekday())
        weekdays = rule.by_day or (first.weekday(),)
        while True:
            start = week + timedelta(weeks=step * rule.interval)
            for weekday in weekdays:
                day = start + timedelta(days=weekday)
                if day >= first:
                    yield day
            step += 1
    while True:
        if rule.freq == "daily":
            yield first + timedelta(days=step * rule.interval)
        elif rule.freq == "monthly":
            months = first.month - 1 + step * rule.interval
            yield _existing(first.year + months // 12, months % 12 + 1, first.day)
        else:
            yield _existing(first.year + step * rule.interval, first.month, first.day)
        step += 1


def _existing(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _zone(name: object) -> ZoneInfo | None:
    """The IANA time zone of that name, or None if there is none."""
    if not isinstance(name, str):
        return None
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, KeyError):
        return None


def _instant(value: object, zone: tzinfo) -> datetime | None:
    """A date and time as an aware UTC instant; a time with no offset is in `zone`."""
    # A date alone is an all-day value, not a time.
    if not isinstance(value, str) or len(value) <= len("2026-01-01"):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _date(value: object) -> date | None:
    """A calendar date, `YYYY-MM-DD` and nothing else."""
    if not isinstance(value, str) or len(value) != len("2026-01-01"):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _midnight(day: date, zone: tzinfo) -> datetime:
    return datetime.combine(day, time()).replace(tzinfo=zone).astimezone(UTC)


def _minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)
