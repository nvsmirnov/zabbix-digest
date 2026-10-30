"""Per-subscription execution state stored as state/<subscription_id>.yaml."""

import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timezone
from pathlib import Path

from common.yamlio import atomic_write_yaml, load_yaml

log = logging.getLogger(__name__)

STATE_FILE_RE = re.compile(r"^([0-9a-f]{1,32})\.yaml$")

RESULT_DELIVERED = "delivered"
RESULT_EMPTY = "empty"
RESULT_ERROR_NOTIFIED = "error_notified"
RESULT_FAILED = "failed"


@dataclass
class SubscriptionState:
    subscription_id: str
    # Date (YYYY-MM-DD) of the last slot that needs no more processing.
    last_completed_slot: str | None = None
    # Result of the last attempt: delivered, empty, error_notified, failed.
    last_result: str | None = None
    # Last delivered digest or empty execution.
    last_success_at: datetime | None = None
    # Last failed attempt (preparation or delivery).
    last_failure_at: datetime | None = None
    last_failure_category: str | None = None
    consecutive_failures: int = 0
    # Set while an attempt runs. A value left after restart means the attempt did not finish.
    attempt_started_at: datetime | None = None
    attempt_slot: str | None = None
    # Date of a completed slot reopened because the subscription time was moved later the same day.
    reopened_slot: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = value.astimezone(timezone.utc).isoformat(timespec="seconds")
        return data

    @classmethod
    def from_dict(cls, subscription_id: str, data: dict) -> "SubscriptionState":
        names = {f.name for f in fields(cls)}
        values = {k: v for k, v in (data or {}).items() if k in names}
        values["subscription_id"] = subscription_id
        for key in ("last_success_at", "last_failure_at", "attempt_started_at"):
            value = values.get(key)
            if isinstance(value, str):
                value = datetime.fromisoformat(value)
            if isinstance(value, datetime) and value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            values[key] = value
        for key in ("last_completed_slot", "attempt_slot", "reopened_slot"):
            if isinstance(values.get(key), date):
                values[key] = values[key].isoformat()
        values["consecutive_failures"] = int(values.get("consecutive_failures") or 0)
        return cls(**values)

    # Transitions.

    def start_attempt(self, slot: date, now: datetime) -> None:
        self.attempt_started_at = now
        self.attempt_slot = slot.isoformat()

    def _finish(self) -> None:
        self.attempt_started_at = None
        self.attempt_slot = None

    def record_success(self, slot: date, now: datetime, result: str) -> None:
        self._finish()
        self.last_completed_slot = slot.isoformat()
        self.reopened_slot = None
        self.last_result = result
        self.last_success_at = now
        self.consecutive_failures = 0

    def record_failure(self, slot: date, now: datetime, category: str, completed: bool) -> None:
        self._finish()
        self.last_result = RESULT_ERROR_NOTIFIED if completed else RESULT_FAILED
        self.last_failure_at = now
        self.last_failure_category = category
        self.consecutive_failures += 1
        if completed:
            self.last_completed_slot = slot.isoformat()
            self.reopened_slot = None


class StateStore:
    """Thread-safe store with in-memory cache. One writer per subscription is ensured by the scheduler."""

    def __init__(self, directory: str) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self._cache: dict[str, SubscriptionState] = {}
        self._lock = threading.Lock()

    def path(self, subscription_id: str) -> Path:
        return self.dir / f"{subscription_id}.yaml"

    def get(self, subscription_id: str) -> SubscriptionState:
        with self._lock:
            cached = self._cache.get(subscription_id)
        if cached is not None:
            return SubscriptionState.from_dict(subscription_id, cached.to_dict())
        path = self.path(subscription_id)
        state = SubscriptionState(subscription_id=subscription_id)
        if path.exists():
            try:
                state = SubscriptionState.from_dict(subscription_id, load_yaml(path) or {})
            except Exception as exc:  # noqa: BLE001
                log.error("state file %s is unreadable, starting with empty state: %s", path.name, exc)
        with self._lock:
            self._cache[subscription_id] = state
        return SubscriptionState.from_dict(subscription_id, state.to_dict())

    def save(self, state: SubscriptionState) -> None:
        atomic_write_yaml(self.path(state.subscription_id), state.to_dict(), mode=0o600)
        with self._lock:
            self._cache[state.subscription_id] = SubscriptionState.from_dict(state.subscription_id, state.to_dict())

    def list_ids(self) -> list[str]:
        result = []
        for entry in self.dir.iterdir():
            match = STATE_FILE_RE.match(entry.name)
            if match and entry.is_file():
                result.append(match.group(1))
        return sorted(result)

    def delete(self, subscription_id: str) -> bool:
        with self._lock:
            self._cache.pop(subscription_id, None)
        try:
            self.path(subscription_id).unlink()
            return True
        except FileNotFoundError:
            return False

    def cleanup(self, keep: set[str], busy: set[str]) -> list[str]:
        """Delete recognized state files of subscriptions that no longer exist."""
        removed = []
        for subscription_id in self.list_ids():
            if subscription_id in keep or subscription_id in busy:
                continue
            if self.delete(subscription_id):
                removed.append(subscription_id)
        return removed
