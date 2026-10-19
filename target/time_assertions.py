"""Validated wall-clock time-of-day ranges for recorded assertions."""

from __future__ import annotations

import re


# A timezone offset such as +08:00 or -05:00 is not an event clock value.
_TIME = re.compile(r"(?<![\d:+-])(?:[01]\d|2[0-3]):[0-5]\d(?::(?:[0-5]\d|\d))?(?![\d:])")


def parse_time_range(prefix: str, duration_seconds: int | None = None) -> tuple[int, int]:
    """Return [start, start + duration) in seconds; ranges may cross midnight.

    A minute prefix denotes its entire minute unless an explicit duration is
    given. A second-precision prefix denotes one second by default.
    """
    if not isinstance(prefix, str) or _TIME.fullmatch(prefix) is None:
        raise ValueError("time_range_invalid_prefix")
    parts = tuple(int(part) for part in prefix.split(":"))
    start = parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) == 3 else 0)
    if duration_seconds is None:
        duration_seconds = 60 if len(parts) == 2 else 1
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or not 1 <= duration_seconds <= 86400:
        raise ValueError("time_range_invalid_duration")
    return start, duration_seconds


def matching_times(text: str, prefix: str, duration_seconds: int | None = None) -> tuple[str, ...]:
    start, duration = parse_time_range(prefix, duration_seconds)
    matches = []
    for match in _TIME.finditer(text):
        value = match.group()
        parts = tuple(int(part) for part in value.split(":"))
        second = parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) == 3 else 0)
        if (second - start) % 86400 < duration:
            matches.append(value)
    return tuple(matches)
