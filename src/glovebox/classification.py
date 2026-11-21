"""The quarantined classifier's seam and the validation of what it proposes (ADR 0002).

The classifier is a plain function of two inputs: one item's text and the operator's
topic list. It has no tool, no credential and no other item's handle to use, and what it
returns is untrusted text. This module is the only place that text is read: it is parsed,
and each of the four semantic fields is checked on its own against its closed
vocabulary. A field that fails becomes `unknown`; the raw output is dropped as soon as
it has been checked and is neither logged nor kept.
"""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from glovebox.fields import ClassificationStatus
from glovebox.formats import RESERVED_TOPICS, TOPIC_UNKNOWN, WINDOW_DAYS, minute

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 2048
# A deadline can lie this far ahead of the classification unless the operator sets a range.
DEFAULT_DEADLINE_DAYS_AHEAD = 365

UNKNOWN = "unknown"
YES_NO = ("yes", "no")
# ISO 8601 with an explicit offset, ASCII digits only; seconds and a fraction are dropped.
DEADLINE_SHAPE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?(Z|[+-][0-9]{2}:[0-9]{2})"
)


@dataclass(frozen=True)
class Topic:
    """One configured topic. Only the ID ever leaves the gate; the name and description
    brief the classifier."""

    id: str
    name: str = field(repr=False)
    description: str = field(repr=False)


class ClassifierUnavailable(Exception):
    """The classifier cannot answer right now. It carries no detail on purpose."""


class Classifier(Protocol):
    def classify(self, text: str, topics: Sequence[Topic]) -> str:
        """The classifier's raw proposal for one item: text in, text out.

        Raises ClassifierUnavailable when it cannot answer.
        """
        ...


@dataclass(frozen=True)
class Classification:
    """The four semantic fields as the gate serves them, and how the classification went."""

    topic: str
    reply_needed: str
    action_requested: str
    deadline: str | None  # a minute timestamp, or None for `unknown`
    status: ClassificationStatus


UNAVAILABLE = Classification(TOPIC_UNKNOWN, UNKNOWN, UNKNOWN, None, "unavailable")
INVALID = Classification(TOPIC_UNKNOWN, UNKNOWN, UNKNOWN, None, "invalid")


@dataclass(frozen=True)
class DeadlineRange:
    """The inclusive range a deadline must fall in. An end that is not set is relative to
    the moment of classification."""

    start: datetime | None = None
    end: datetime | None = None

    def bounds(self, now: datetime) -> tuple[datetime, datetime]:
        start = self.start or now - timedelta(days=WINDOW_DAYS)
        end = self.end or now + timedelta(days=DEFAULT_DEADLINE_DAYS_AHEAD)
        return start, end


class QuarantinedClassifier:
    """Runs the classifier for one item at a time and releases only validated fields.

    Each item is its own quarantine: whatever one item's classification does, including
    failing in any way, changes nothing about another's.
    """

    def __init__(
        self,
        backend: Classifier | None,
        topics: Sequence[Topic],
        deadline_range: DeadlineRange,
    ) -> None:
        self._backend = backend
        self._topics = tuple(topics)
        self._range = deadline_range

    def classify(self, text: str, now: datetime) -> Classification:
        if self._backend is None:
            return UNAVAILABLE
        try:
            raw = self._backend.classify(text, self._topics)
        except ClassifierUnavailable:
            logger.warning("classifier unavailable")
            return UNAVAILABLE
        except Exception as error:
            # Only the type is logged: an exception message may hold the item's text.
            logger.warning("classifier failed: %s", type(error).__name__)
            return UNAVAILABLE
        return validate(raw, {topic.id for topic in self._topics}, *self._range.bounds(now))


def validate(
    raw: object, topic_ids: set[str], earliest: datetime, latest: datetime
) -> Classification:
    """Check a raw proposal field by field. The output holds nothing of `raw` itself:
    every value in it is a constant of this module, a configured topic ID or a parsed
    minute."""
    proposal = _parse(raw)
    if proposal is None:
        return INVALID
    topic = _topic(proposal.get("topic"), topic_ids)
    reply_needed = _yes_no(proposal.get("reply_needed"))
    action_requested = _yes_no(proposal.get("action_requested"))
    deadline_valid, deadline = _deadline(proposal.get("deadline"), earliest, latest)
    valid = [
        topic is not None,
        reply_needed is not None,
        action_requested is not None,
        deadline_valid,
    ]
    status: ClassificationStatus = (
        "classified" if all(valid) else "partial" if any(valid) else "invalid"
    )
    return Classification(
        topic=topic or TOPIC_UNKNOWN,
        reply_needed=reply_needed or UNKNOWN,
        action_requested=action_requested or UNKNOWN,
        deadline=deadline,
        status=status,
    )


def _parse(raw: object) -> dict[str, object] | None:
    """The proposal as an object, or None for prose, other JSON or anything oversized."""
    if not isinstance(raw, str) or len(raw) > MAX_OUTPUT_CHARS:
        return None
    try:
        proposal = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return proposal if isinstance(proposal, dict) else None


def _topic(value: object, topic_ids: set[str]) -> str | None:
    if isinstance(value, str) and (value in topic_ids or value in RESERVED_TOPICS):
        return value
    return None


def _yes_no(value: object) -> str | None:
    # `unknown` is a legitimate answer; it is simply what the field would be anyway.
    if isinstance(value, str) and (value in YES_NO or value == UNKNOWN):
        return value
    return None


def _deadline(value: object, earliest: datetime, latest: datetime) -> tuple[bool, str | None]:
    """Whether the proposal was valid, and the minute it names (None for `unknown`)."""
    if value == UNKNOWN:
        return True, None
    if not isinstance(value, str) or DEADLINE_SHAPE.fullmatch(value) is None:
        return False, None
    try:
        parsed = datetime.fromisoformat(value).astimezone(UTC).replace(second=0, microsecond=0)
    except (ValueError, OverflowError):
        return False, None
    if not earliest.replace(second=0, microsecond=0) <= parsed <= latest:
        return False, None
    return True, minute(parsed)
