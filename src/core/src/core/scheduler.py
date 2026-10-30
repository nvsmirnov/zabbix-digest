"""Internal scheduler: finds due subscriptions and runs them with a worker limit."""

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from common.logs import log_context
from common.sentry import capture_exception
from .config import SchedulerConfig
from .metrics import CoreMetrics
from .processing import Processor
from .schedule import apply_time_change, due_slot
from .source import UsersSource
from .state import StateStore
from .users import UsersConfig

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        config: SchedulerConfig,
        tz: ZoneInfo,
        source: UsersSource,
        state: StateStore,
        processor: Processor,
        metrics: CoreMetrics,
    ) -> None:
        self.config = config
        self.tz = tz
        self.source = source
        self.state = state
        self.processor = processor
        self.metrics = metrics
        self.running: dict[str, asyncio.Task] = {}
        self._workers = asyncio.Semaphore(config.max_workers)
        self.accepting = True
        source.listeners.append(self._on_config_change)

    def busy_ids(self) -> set[str]:
        return set(self.running)

    def due(self, now: datetime | None = None, only: set[str] | None = None) -> list:
        config = self.source.current
        if config is None:
            return []
        now = now or datetime.now(timezone.utc)
        result = []
        for user, sub in config.iter_subscriptions():
            if not sub.enabled or sub.id in self.running:
                continue
            if only is not None and sub.id not in only:
                continue
            slot = due_slot(
                sub.time, self.state.get(sub.id), now, self.tz,
                self.config.delivery_window, self.config.retry_interval,
            )
            if slot is not None:
                result.append((user, sub, slot))
        return result

    def tick(self, now: datetime | None = None, only: set[str] | None = None) -> list[asyncio.Task]:
        if not self.accepting:
            return []
        tasks = []
        for user, sub, slot in self.due(now, only):
            # The task gets the configuration snapshot taken now.
            task = asyncio.create_task(self._run(user, sub, slot), name=f"subscription-{sub.id}")
            self.running[sub.id] = task
            task.add_done_callback(lambda t, sid=sub.id: self.running.pop(sid, None))
            tasks.append(task)
        return tasks

    async def force(self, only: set[str] | None = None) -> dict[str, str]:
        """Send digests of enabled subscriptions now, ignoring the schedule. State is not changed."""
        config = self.source.current
        if config is None:
            return {}
        targets = [
            (user, sub) for user, sub in config.iter_subscriptions()
            if sub.enabled and (only is None or sub.id in only)
        ]

        async def one(user, sub) -> tuple[str, str]:
            async with self._workers:
                return sub.id, await self.processor.run_forced(user, sub)

        return dict(await asyncio.gather(*(one(user, sub) for user, sub in targets)))

    async def _run(self, user, sub, slot) -> str:
        with log_context(user=user.email, subscription_id=sub.id):
            return await self._run_one(user, sub, slot)

    async def _run_one(self, user, sub, slot) -> str:
        async with self._workers:
            if not self.accepting:
                return "interrupted"
            self.metrics.active_workers.inc()
            try:
                return await self.processor.run_scheduled(user, sub, slot)
            except asyncio.CancelledError:
                log.info("processing interrupted", extra={"operation": "scheduled"})
                raise
            except Exception as exc:  # noqa: BLE001
                self.metrics.unexpected_errors.inc()
                log.exception("unexpected error in subscription processing", extra={"operation": "scheduled"})
                capture_exception(exc)
                return "error"
            finally:
                self.metrics.active_workers.dec()

    async def run(self, stop: asyncio.Event) -> None:
        self.metrics.scheduler_ready.set(1)
        log.info("scheduler started (time zone %s)", self.tz.key)
        try:
            while not stop.is_set():
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001
                    self.metrics.unexpected_errors.inc()
                    log.exception("scheduler tick failed")
                    capture_exception(exc)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.config.tick_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.metrics.scheduler_ready.set(0)

    async def shutdown(self, timeout: float) -> None:
        """Stop new work, interrupt preparation, let active deliveries finish until the deadline."""
        self.accepting = False
        self.processor.shutting_down = True
        tasks = list(self.running.values())
        for sid, task in list(self.running.items()):
            if sid not in self.processor.delivering:
                task.cancel()
        if not tasks:
            return
        log.info("waiting for %d active task(s)", len(tasks))
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            log.warning("shutdown deadline reached, %d task(s) cancelled", len(pending))
            await asyncio.wait(pending, timeout=5)

    def cleanup(self) -> list[str]:
        config = self.source.current
        if config is None:
            return []
        removed = self.state.cleanup(config.subscription_ids(), self.busy_ids())
        if removed:
            log.info("removed %d obsolete state file(s)", len(removed))
        return removed

    def _on_config_change(self, old: UsersConfig | None, new: UsersConfig) -> None:
        if old is None:
            return
        gone = old.subscription_ids() - new.subscription_ids()
        for sid in gone:
            if sid not in self.running:
                self.state.delete(sid)
        now = datetime.now(timezone.utc)
        for user, sub in new.iter_subscriptions():
            found = old.find_subscription(sub.id)
            if found is None or found[1].time == sub.time:
                continue
            state = self.state.get(sub.id)
            if apply_time_change(state, sub.time, now, self.tz):
                self.state.save(state)
                with log_context(user=user.email, subscription_id=sub.id):
                    if state.reopened_slot:
                        log.info("time changed to %s: digest is sent again today at the new time", sub.time,
                                 extra={"operation": "config"})
                    else:
                        log.info("time changed to %s (already passed today): today's digest is not repeated",
                                 sub.time, extra={"operation": "config"})
