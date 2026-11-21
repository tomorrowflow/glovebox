"""The shapes of the values that cross the gate, shared by the store that mints them and
the output validator that checks them."""

HANDLE_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # a-z and 2-7
HANDLE_LENGTH = 24
HANDLE_PATTERN = rf"^[a-z2-7]{{{HANDLE_LENGTH}}}$"

MINUTE_FORMAT = "%Y-%m-%dT%H:%MZ"
MINUTE_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$"
MINUTE_LENGTH = 17
