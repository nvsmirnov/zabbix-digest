"""Subscription ID generation."""

import secrets
from typing import Iterable

MAX_LENGTH = 32


class IdSpaceExhausted(Exception):
    pass


def new_subscription_id(existing: Iterable[str], length: int = 4) -> str:
    """Random lowercase hex ID of `length` characters, unique across `existing` (any length)."""
    if not 1 <= length <= MAX_LENGTH:
        raise ValueError(f"ID length must be between 1 and {MAX_LENGTH}")
    taken = set(existing)
    space = 16**length
    same_length = sum(1 for i in taken if len(i) == length)
    if same_length >= space:
        raise IdSpaceExhausted(f"no free {length}-character subscription IDs left")
    # Random tries first; fall back to a scan for nearly full spaces.
    for _ in range(min(1000, space * 4)):
        candidate = secrets.token_hex((length + 1) // 2)[:length]
        if candidate not in taken:
            return candidate
    start = secrets.randbelow(space)
    for offset in range(space):
        candidate = f"{(start + offset) % space:0{length}x}"
        if candidate not in taken:
            return candidate
    raise IdSpaceExhausted(f"no free {length}-character subscription IDs left")
