"""Closed value types shared by tool inputs and outputs.

No type here is a free string: each is a handle, a minute-precision timestamp, a count or a
page cursor, with a fixed length and an anchored pattern.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, StringConstraints

from glovebox.formats import (
    COUNT_MAX_LENGTH,
    COUNT_PATTERN,
    CURSOR_LENGTH,
    CURSOR_PATTERN,
    HANDLE_LENGTH,
    HANDLE_PATTERN,
    MINUTE_FORMAT,
    MINUTE_LENGTH,
    MINUTE_PATTERN,
    TOPIC_ID_MAX_LENGTH,
    TOPIC_ID_PATTERN,
)


def _is_a_minute(value: str) -> str:
    datetime.strptime(value, MINUTE_FORMAT)
    return value


Handle = Annotated[
    str,
    StringConstraints(pattern=HANDLE_PATTERN, min_length=HANDLE_LENGTH, max_length=HANDLE_LENGTH),
]
Minute = Annotated[
    str,
    StringConstraints(pattern=MINUTE_PATTERN, min_length=MINUTE_LENGTH, max_length=MINUTE_LENGTH),
    AfterValidator(_is_a_minute),
]
TopicId = Annotated[
    str, StringConstraints(pattern=TOPIC_ID_PATTERN, min_length=1, max_length=TOPIC_ID_MAX_LENGTH)
]
YesNoUnknown = Literal["yes", "no", "unknown"]
# How far a classification got: every field valid, some valid, none valid, or no answer.
ClassificationStatus = Literal["classified", "partial", "invalid", "unavailable"]
# A deadline is a minute timestamp, or `unknown`.
Deadline = Minute | Literal["unknown"]
# "0" to "1000", or "1000+" for anything above the cap.
Count = Annotated[
    str, StringConstraints(pattern=COUNT_PATTERN, min_length=1, max_length=COUNT_MAX_LENGTH)
]
Cursor = Annotated[
    str,
    StringConstraints(pattern=CURSOR_PATTERN, min_length=CURSOR_LENGTH, max_length=CURSOR_LENGTH),
]
