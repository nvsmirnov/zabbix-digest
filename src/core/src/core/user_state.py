"""Per-user state for reminders, stored as state/users/<key>.yaml.

The key is a hash of the lowercase email, so email addresses are not part of file names.
"""

import hashlib
import logging
import re
import threading
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from common.yamlio import atomic_write_yaml, load_yaml

log = logging.getLogger(__name__)

USER_FILE_RE = re.compile(r"^([0-9a-f]{16})\.yaml$")
TIMESTAMPS = ("last_contact_at", "since", "last_reminder_at", "last_reminder_failure_at")


def user_key(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


@dataclass
class UserState:
    email: str
    # Last message recorded for the user: digest, error notification or reminder (not tests).
    last_contact_at: datetime | None = None
    # When the reminder setting was turned on; counts as a contact for the first period.
    since: datetime | None = None
    # Reminder setting seen last time (to detect turning it on).
    reminder: str | None = None
    last_reminder_at: datetime | None = None
    last_reminder_failure_at: datetime | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in TIMESTAMPS:
            if data[key] is not None:
                data[key] = data[key].astimezone(timezone.utc).isoformat(timespec="seconds")
        return data

    @classmethod
    def from_dict(cls, email: str, data: dict) -> "UserState":
        names = {f.name for f in fields(cls)}
        values = {k: v for k, v in (data or {}).items() if k in names}
        values["email"] = email
        for key in TIMESTAMPS:
            value = values.get(key)
            if isinstance(value, str):
                value = datetime.fromisoformat(value)
            if isinstance(value, datetime) and value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            values[key] = value
        return cls(**values)

    def last_contact(self) -> datetime | None:
        values = [v for v in (self.last_contact_at, self.since) if v is not None]
        return max(values) if values else None


class UserStateStore:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, email: str) -> Path:
        return self.dir / f"{user_key(email)}.yaml"

    def get(self, email: str) -> UserState:
        path = self.path(email)
        if path.exists():
            try:
                return UserState.from_dict(email, load_yaml(path) or {})
            except Exception as exc:  # noqa: BLE001
                log.error("user state file %s is unreadable, starting with empty state: %s", path.name, exc)
        return UserState(email=email)

    def save(self, state: UserState) -> None:
        with self._lock:
            atomic_write_yaml(self.path(state.email), state.to_dict(), mode=0o600)

    def update(self, email: str, change) -> UserState:
        """Read, change and save under the lock (several tasks may record contacts at once)."""
        with self._lock:
            state = self.get(email)
            change(state)
            atomic_write_yaml(self.path(email), state.to_dict(), mode=0o600)
            return state

    def record_contact(self, email: str, when: datetime) -> None:
        def change(state: UserState) -> None:
            if state.last_contact_at is None or when > state.last_contact_at:
                state.last_contact_at = when

        self.update(email, change)

    def cleanup(self, keep_emails: set[str]) -> list[str]:
        """Delete files of users that no longer exist."""
        keep = {user_key(e) for e in keep_emails}
        removed = []
        for entry in self.dir.iterdir():
            match = USER_FILE_RE.match(entry.name)
            if match and entry.is_file() and match.group(1) not in keep:
                try:
                    entry.unlink()
                    removed.append(match.group(1))
                except FileNotFoundError:
                    pass
        return removed
