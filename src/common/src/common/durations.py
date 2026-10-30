"""Human-readable durations such as "30s", "5m", "2h"."""

import re
from typing import Annotated, Any

from pydantic import BeforeValidator

_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")


def parse_duration(value: Any) -> float:
    """Return seconds. Accepts numbers (seconds) and strings like "1h30m"."""
    if isinstance(value, bool):
        raise ValueError("invalid duration")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("duration must not be negative")
        return float(value)
    if not isinstance(value, str):
        raise ValueError("invalid duration")
    text = value.strip().lower().replace(" ", "")
    if not text:
        raise ValueError("invalid duration")
    try:
        return parse_duration(float(text))
    except ValueError:
        pass
    pos = 0
    total = 0.0
    for match in _PART_RE.finditer(text):
        if match.start() != pos:
            raise ValueError("invalid duration")
        total += float(match.group(1)) * _UNITS[match.group(2)]
        pos = match.end()
    if pos != len(text):
        raise ValueError("invalid duration")
    return total


Duration = Annotated[float, BeforeValidator(parse_duration)]
