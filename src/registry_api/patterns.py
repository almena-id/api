"""The patterns a tenant writes for its own text fields, run on what anyone
types into its forms.

They run under the `regex` module with a time limit: a pattern that backtracks
without end (`(a+)+$`) fails the answer instead of holding the server.
"""

import regex

# Seconds one match may take; answers are a few thousand characters at most.
TIMEOUT = 0.05


def valid(pattern: str) -> bool:
    """Whether `pattern` is a pattern at all."""
    try:
        regex.compile(pattern)
    except regex.error:
        return False
    return True


def matches(pattern: str, text: str) -> bool:
    """Whether `text` matches `pattern` whole; one that takes too long does not."""
    try:
        return regex.fullmatch(pattern, text, timeout=TIMEOUT) is not None
    except (TimeoutError, regex.error):
        return False
