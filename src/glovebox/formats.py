"""The shapes of the values that cross the gate, shared by the store that mints them and
the output validator that checks them."""

HANDLE_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # a-z and 2-7
HANDLE_LENGTH = 24
HANDLE_PATTERN = rf"^[a-z2-7]{{{HANDLE_LENGTH}}}$"

MINUTE_FORMAT = "%Y-%m-%dT%H:%MZ"
# [0-9], not \d: the validator's regex engine takes \d to match digits of any script.
MINUTE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z$"
MINUTE_LENGTH = 17

# Limits that bound side channels (ADR 0001).
WINDOW_DAYS = 30  # mail is neither ingested nor served beyond this
MAX_ITEMS = 50  # items or groups in one response
COUNT_CAP = 1000  # a count above this is reported as COUNT_CAPPED

COUNT_CAPPED = f"{COUNT_CAP}+"
# No leading zeros: a count has one spelling.
COUNT_PATTERN = rf"^(0|[1-9][0-9]{{0,2}}|{COUNT_CAP}|{COUNT_CAP}\+)$"
COUNT_MAX_LENGTH = len(COUNT_CAPPED)

# A page cursor: 4 bytes of offset and 16 of MAC, base64url without padding.
CURSOR_LENGTH = 27
CURSOR_PATTERN = rf"^[A-Za-z0-9_-]{{{CURSOR_LENGTH}}}$"
