"""Authoritative user configuration storage (users.yaml) with revision checks.

Every change follows the same sequence:
  read -> modify a copy -> revision + 1 -> Core validates -> revision unchanged on disk? -> atomic write -> notify Core.
"""

import asyncio
import copy
import logging
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

from common.errors import describe_error
from common.yamlio import atomic_write_yaml, load_yaml
from .core_client import ConfigRejected, CoreClient, CoreUnavailable
from .metrics import WebMetrics

log = logging.getLogger(__name__)

T = TypeVar("T")

NO_CHANGE = object()


class RevisionConflict(Exception):
    pass


class StoreError(Exception):
    pass


class UsersStore:
    def __init__(self, path: str, core: CoreClient, metrics: WebMetrics) -> None:
        self.path = Path(path)
        self.core = core
        self.metrics = metrics
        self._lock = asyncio.Lock()
        self._notify_tasks: set[asyncio.Task] = set()

    def read(self) -> dict:
        if not self.path.exists():
            return {"revision": 0, "users": []}
        try:
            data = load_yaml(self.path) or {}
        except Exception as exc:  # noqa: BLE001
            raise StoreError(f"cannot read {self.path.name}: {describe_error(exc)}") from None
        if not isinstance(data, dict):
            raise StoreError(f"{self.path.name} must contain a mapping")
        data.setdefault("revision", 0)
        data.setdefault("users", [])
        return data

    def accessible(self) -> bool:
        directory = self.path.parent
        return directory.is_dir() and os.access(directory, os.W_OK) and (
            not self.path.exists() or os.access(self.path, os.R_OK | os.W_OK)
        )

    async def mutate(self, change: Callable[[dict], T]) -> T:
        """Apply `change` to a copy of the configuration and save it after Core validation.

        `change` may raise to abort, or return NO_CHANGE to skip saving.
        Raises ConfigRejected, CoreUnavailable, RevisionConflict, StoreError.
        """
        async with self._lock:
            current = self.read()
            base = int(current.get("revision") or 0)
            proposed = copy.deepcopy(current)
            result = change(proposed)
            if result is NO_CHANGE:
                return result
            proposed["revision"] = base + 1
            try:
                await self.core.validate(proposed)
            except ConfigRejected:
                self.metrics.config_save_failures.labels(reason="rejected").inc()
                raise
            except CoreUnavailable:
                self.metrics.config_save_failures.labels(reason="core_unavailable").inc()
                raise
            if int(self.read().get("revision") or 0) != base:
                self.metrics.config_save_failures.labels(reason="conflict").inc()
                raise RevisionConflict()
            try:
                atomic_write_yaml(self.path, proposed, mode=0o600)
            except OSError as exc:
                self.metrics.config_save_failures.labels(reason="write").inc()
                raise StoreError(f"cannot write configuration: {exc.strerror}") from None
            log.info("user configuration saved (revision %d)", proposed["revision"])
        task = asyncio.create_task(self.core.notify())
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)
        return result


def find_user(data: dict, *, email: str | None = None, generation: str | None = None) -> dict | None:
    for user in data.get("users") or []:
        if email is not None and str(user.get("email", "")).lower() == email.lower():
            return user
        if generation is not None and user.get("generation") == generation:
            return user
    return None


def find_subscription_by_token(data: dict, token: str) -> tuple[dict, dict] | None:
    for user in data.get("users") or []:
        for sub in user.get("subscriptions") or []:
            if sub.get("unsubscribe_token") and sub["unsubscribe_token"] == token:
                return user, sub
    return None


def all_subscription_ids(data: dict) -> set[str]:
    return {str(s.get("id")) for u in data.get("users") or [] for s in u.get("subscriptions") or []}


def subscription_title(sub: dict) -> str:
    name = (sub.get("name") or "").strip()
    return f"{name} (#{sub['id']})" if name else f"#{sub['id']}"


def public_user(user: dict) -> dict[str, Any]:
    """Profile fields sent to Core for preview and test send."""
    return {"email": user["email"], "zabbix_token": user["zabbix_token"]}
