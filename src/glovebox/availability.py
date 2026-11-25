"""Busy and free windows and overlaps over stored events: pure functions of minute strings.

Times are `YYYY-MM-DDTHH:MMZ`, which sort the way the moments they name do, so they are
compared as they are. An event overlaps a range when it starts before the range ends and ends
after the range starts, so one that only touches it does not.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from glovebox.calendars import look_ahead
from glovebox.formats import minute


def look_ahead_range(
    now: datetime, range_from: str | None, range_to: str | None
) -> tuple[str, str]:
    """The range asked for, cut to the look-ahead; a missing end is the look-ahead's own.

    The result is empty (the start is not before the end) when nothing of the range is
    inside the look-ahead, or when the range ends before it starts.
    """
    first, last = (minute(moment) for moment in look_ahead(now))
    return max(range_from or first, first), min(range_to or last, last)


def windows(blocking: Iterable[tuple[str, str]], start: str, end: str) -> list[dict[str, str]]:
    """The range from `start` to `end` split into busy windows, where any of the `blocking`
    intervals is, and free windows between them; events that run into each other or touch
    make one busy window."""
    merged: list[tuple[str, str]] = []
    for begin, finish in sorted(blocking):
        begin, finish = max(begin, start), min(finish, end)
        if finish <= begin:
            continue
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    result: list[dict[str, str]] = []
    reached = start
    for begin, finish in merged:
        if reached < begin:
            result.append({"start": reached, "end": begin, "state": "free"})
        result.append({"start": begin, "end": finish, "state": "busy"})
        reached = finish
    if reached < end:
        result.append({"start": reached, "end": end, "state": "free"})
    return result


def overlapping(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The events that overlap another of them, in the order given.

    `events` are sorted by start. A run of events each starting before the latest end so far
    is one cluster, and every event of a cluster of two or more overlaps one of the others.
    """
    overlapping_events: list[Mapping[str, Any]] = []
    cluster: list[Mapping[str, Any]] = []
    reach = ""
    for event in events:
        if cluster and event["start"] < reach:
            cluster.append(event)
            reach = max(reach, event["end"])
            continue
        if len(cluster) > 1:
            overlapping_events += cluster
        cluster, reach = [event], event["end"]
    if len(cluster) > 1:
        overlapping_events += cluster
    return overlapping_events
