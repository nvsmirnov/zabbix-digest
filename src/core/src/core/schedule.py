"""Daily slot calculation.

A slot is identified by its local calendar date. The slot of date D starts at D + subscription time
in the Core time zone and may be processed until start + delivery_window.

DST rules (zoneinfo, fold=0):
  - Nonexistent local time (clock jumps forward): the slot starts at the same UTC instant as the
    time with the pre-transition offset, i.e. shifted forward by the gap (02:30 -> 03:30).
  - Ambiguous local time (clock jumps back): the first occurrence is used.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .state import SubscriptionState


def parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def slot_start(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    local = datetime.combine(day, parse_hhmm(hhmm)).replace(tzinfo=tz, fold=0)
    return local.astimezone(timezone.utc)


def apply_time_change(state: SubscriptionState, new_hhmm: str, now: datetime, tz: ZoneInfo) -> bool:
    """Update the state after the subscription time was changed. Returns True when changed.

    - Today's slot is completed and the new time is still ahead today: the slot is reopened,
      so the digest is sent again today at the new time.
    - A reopened slot and the time is changed again to a moment already passed: the slot is
      closed again (no immediate second digest).
    """
    now = now.astimezone(timezone.utc)
    today = now.astimezone(tz).date()
    ahead = slot_start(today, new_hhmm, tz) > now
    if state.last_completed_slot == today.isoformat() and ahead:
        state.last_completed_slot = (today - timedelta(days=1)).isoformat()
        state.reopened_slot = today.isoformat()
        return True
    if state.reopened_slot == today.isoformat() and not ahead:
        state.last_completed_slot = today.isoformat()
        state.reopened_slot = None
        return True
    return False


def due_slot(
    hhmm: str,
    state: SubscriptionState,
    now: datetime,
    tz: ZoneInfo,
    window: float,
    retry_interval: float,
) -> date | None:
    """Return the date of the slot that should be processed now, or None."""
    now = now.astimezone(timezone.utc)
    today = now.astimezone(tz).date()
    for day in (today - timedelta(days=1), today):
        start = slot_start(day, hhmm, tz)
        if not (start <= now < start + timedelta(seconds=window)):
            continue
        if state.last_completed_slot and state.last_completed_slot >= day.isoformat():
            continue
        if (
            state.last_failure_at
            and state.last_failure_at >= start
            and now < state.last_failure_at + timedelta(seconds=retry_interval)
        ):
            continue
        return day
    return None
