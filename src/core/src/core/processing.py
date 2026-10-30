"""Report preparation and delivery for scheduled runs, preview, test send and send now."""

import logging
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from common.logs import log_context
from common.sentry import capture_exception
from .config import CoreSettings
from .errors import Category, PreparationError, RenderError
from .messages import digest_message, error_message, test_message
from .metrics import CoreMetrics
from .state import RESULT_DELIVERED, RESULT_EMPTY, StateStore
from .transports.base import DeliveryFailed, TransportRegistry
from .users import Subscription, User
from .zabbix.client import ZabbixClient
from .zabbix.identity import validate_user
from .zabbix.problems import fetch_problems, parse_ref

log = logging.getLogger(__name__)


class ShuttingDown(Exception):
    """Delivery was not started because shutdown is in progress."""


class Processor:
    def __init__(
        self,
        settings: CoreSettings,
        tz: ZoneInfo,
        zabbix: ZabbixClient,
        transports: TransportRegistry,
        state: StateStore,
        metrics: CoreMetrics,
    ) -> None:
        self.settings = settings
        self.tz = tz
        self.zabbix = zabbix
        self.transports = transports
        self.state = state
        self.metrics = metrics
        # Subscription IDs with a delivery in progress; shutdown lets them finish.
        self.delivering: set[str] = set()
        self.shutting_down = False

    # Preparation.

    async def prepare_digest(self, user: User, sub: Subscription, now: float | None = None) -> dict:
        """Return a digest message (possibly with zero problems). Raises PreparationError."""
        now = time.time() if now is None else now
        try:
            if sub.transport not in self.transports.names():
                raise PreparationError(Category.INTERNAL, f"transport '{sub.transport}' is not configured")
            ref = parse_ref(sub.widget_url)
            identity = await validate_user(self.zabbix, user.zabbix_token, user.email)
            problems = await fetch_problems(
                self.zabbix,
                user.zabbix_token,
                identity.userid,
                ref,
                self.settings.zabbix.max_problems,
                self.settings.zabbix.ignore_unknown_widget_fields,
                now=now,
                overrides=sub.overrides,
            )
            return digest_message(self.settings, self.tz, user, sub, problems, now)
        except PreparationError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.metrics.unexpected_errors.inc()
            log.exception("unexpected error while preparing the report", extra=self._ctx(sub, "prepare"))
            capture_exception(exc)
            raise PreparationError(Category.INTERNAL) from None

    # Delivery.

    async def deliver(self, sub: Subscription, message: dict) -> None:
        """Raises RenderError, DeliveryFailed or ShuttingDown."""
        if self.shutting_down:
            raise ShuttingDown()
        self.delivering.add(sub.id)
        try:
            await self.transports.deliver(sub.transport, message, stop_retrying=lambda: self.shutting_down)
        finally:
            self.delivering.discard(sub.id)

    # Scheduled run.

    async def run_scheduled(self, user: User, sub: Subscription, slot: date) -> str:
        """Process one slot and persist the result. Returns the execution result label."""
        with log_context(user=user.email, subscription_id=sub.id):
            return await self._run_scheduled(user, sub, slot)

    async def _run_scheduled(self, user: User, sub: Subscription, slot: date) -> str:
        started = time.monotonic()
        state = self.state.get(sub.id)
        state.start_attempt(slot, _now())
        self.state.save(state)
        try:
            result, category = await self._execute(user, sub, self._ctx(sub, "scheduled"))
        finally:
            self.metrics.processing_duration.observe(time.monotonic() - started)
        if result != "interrupted":
            state = self.state.get(sub.id)
            if result in ("delivered", "empty"):
                state.record_success(slot, _now(), RESULT_DELIVERED if result == "delivered" else RESULT_EMPTY)
            else:
                state.record_failure(slot, _now(), category or "delivery", completed=result == "error_notified")
            self.state.save(state)
        self.metrics.executions.labels(result=result).inc()
        return result

    async def run_forced(self, user: User, sub: Subscription) -> str:
        """Prepare and deliver a digest now, ignoring the schedule. The execution state is not changed."""
        with log_context(user=user.email, subscription_id=sub.id):
            result, _ = await self._execute(user, sub, self._ctx(sub, "forced"))
        self.metrics.operations.labels(operation="forced", result=result).inc()
        return result

    async def _execute(self, user: User, sub: Subscription, ctx: dict) -> tuple[str, str | None]:
        """Prepare and deliver a digest (or an error notification). Returns (result, failure category).

        Results: delivered, empty, error_notified, failed, interrupted.
        """
        try:
            message = await self.prepare_digest(user, sub)
        except PreparationError as exc:
            return await self._notify_error(user, sub, exc, ctx)

        if message["digest"]["total"] == 0 and message["digest"]["total_exact"]:
            log.info("no active problems, digest not sent", extra=ctx)
            return "empty", None

        try:
            await self.deliver(sub, message)
        except RenderError as exc:
            return await self._notify_error(user, sub, PreparationError(Category.RENDER, str(exc)), ctx)
        except DeliveryFailed as exc:
            log.error("digest delivery failed: %s", exc.reason, extra={**ctx, "error_category": "delivery"})
            return "failed", "delivery"
        except ShuttingDown:
            log.info("shutdown in progress, digest delivery not started", extra=ctx)
            return "interrupted", None
        log.info(
            "digest delivered (%d of %d problems)", message["digest"]["shown"], message["digest"]["total"], extra=ctx
        )
        return "delivered", None

    async def _notify_error(
        self, user: User, sub: Subscription, error: PreparationError, ctx: dict
    ) -> tuple[str, str | None]:
        self.metrics.preparation_errors.labels(category=error.category).inc()
        ctx = {**ctx, "error_category": error.category}
        log.warning("report preparation failed: %s", error, extra=ctx)
        message = error_message(self.settings, self.tz, user, sub, error, time.time())
        try:
            await self.deliver(sub, message)
        except (RenderError, DeliveryFailed) as exc:
            log.error("error notification delivery failed: %s", exc, extra=ctx)
            return "failed", error.category
        except ShuttingDown:
            return "interrupted", None
        log.info("error notification delivered", extra=ctx)
        return "error_notified", error.category

    # Preview, test send and send now. They never change the execution state.

    async def preview(self, user: User, sub: Subscription) -> dict:
        """Raises PreparationError, PreviewNotSupported, RenderError."""
        message = await self.prepare_digest(user, sub)
        return self.transports.render_preview(sub.transport, message)

    async def send_now(self, user: User, sub: Subscription) -> dict:
        """Prepare and deliver a digest now (frontend request).

        Unlike a forced run, preparation errors are returned to the caller instead of
        sending an error notification. An empty report is not sent.
        Raises PreparationError, RenderError, DeliveryFailed, ShuttingDown.
        """
        message = await self.prepare_digest(user, sub)
        d = message["digest"]
        summary = {"shown": d["shown"], "total": d["total"], "total_exact": d["total_exact"]}
        if d["total"] == 0 and d["total_exact"]:
            log.info("no active problems, digest not sent", extra=self._ctx(sub, "send_now"))
            return {"result": "empty", **summary}
        await self.deliver(sub, message)
        log.info("digest delivered (%d of %d problems)", d["shown"], d["total"], extra=self._ctx(sub, "send_now"))
        return {"result": "delivered", **summary}

    async def test_send(self, user: User, sub: Subscription) -> None:
        """Raises RenderError, DeliveryFailed, ShuttingDown."""
        message = test_message(self.settings, self.tz, user, sub, time.time())
        await self.deliver(sub, message)

    def _ctx(self, sub: Subscription, operation: str) -> dict:
        # user and subscription_id come from log_context (set by the callers).
        return {"operation": operation, "transport": sub.transport}


def _now() -> datetime:
    return datetime.now(timezone.utc)
