"""Optional reminder: a reminder that the service works when no messages were sent for a period.

Rules:
  - Sent by email to the profile address through the global smtp section (not through transports).
  - Only for users with reminder set and at least one enabled subscription.
  - Only after today's processing of the user is over: every enabled subscription has started
    today's slot and completed it, or its delivery window has ended.
  - Only when the last result of every enabled subscription is a successful check
    (delivered or empty): the report means "everything was checked, nothing found".
  - Only when at least the period passed (local calendar days) since the last contact: any
    recorded digest, error notification or reminder, or the moment the setting was turned on.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jinja2 import TemplateError

from common.errors import describe_error
from common.logs import log_context
from .config import CoreSettings
from .errors import RenderError
from .metrics import CoreMetrics
from .schedule import slot_start
from .state import RESULT_DELIVERED, RESULT_EMPTY, StateStore, SubscriptionState
from .system_mail import SystemMailer
from .transports.email import html_to_text, make_environment
from .user_state import UserState, UserStateStore
from .users import User

log = logging.getLogger(__name__)

PERIOD_DAYS = {"weekly": 7, "monthly": 30}
OK_RESULTS = (RESULT_DELIVERED, RESULT_EMPTY)


def blocked_reason(user: User, states: dict[str, SubscriptionState]) -> str | None:
    """Why no reminder can be sent now regardless of dates: no_subscriptions, not_checked, errors."""
    enabled = [s for s in user.subscriptions if s.enabled]
    if not enabled:
        return "no_subscriptions"
    results = [states[s.id].last_result for s in enabled]
    if any(r not in OK_RESULTS and r is not None for r in results):
        return "errors"
    if any(r is None for r in results):
        return "not_checked"
    return None


def day_finished(user: User, states: dict[str, SubscriptionState], now: datetime, tz: ZoneInfo, window: float) -> bool:
    """True when every enabled subscription is done with today's slot."""
    today = now.astimezone(tz).date()
    for sub in user.subscriptions:
        if not sub.enabled:
            continue
        start = slot_start(today, sub.time, tz)
        if now < start:
            return False
        completed = (states[sub.id].last_completed_slot or "") >= today.isoformat()
        if not completed and now < start + timedelta(seconds=window):
            return False
    return True


def next_date(user: User, user_state: UserState, now: datetime, tz: ZoneInfo) -> date | None:
    """Earliest local date of the next reminder (dates only; other conditions may delay it)."""
    days = PERIOD_DAYS.get(user.reminder or "")
    if days is None:
        return None
    last = user_state.last_contact()
    base = last.astimezone(tz).date() if last else now.astimezone(tz).date()
    return base + timedelta(days=days)


def reminder_due(
    user: User,
    states: dict[str, SubscriptionState],
    user_state: UserState,
    now: datetime,
    tz: ZoneInfo,
    window: float,
    retry_interval: float,
) -> bool:
    if user.reminder not in PERIOD_DAYS or user_state.last_contact() is None:
        return False
    if blocked_reason(user, states) is not None:
        return False
    if not day_finished(user, states, now, tz, window):
        return False
    failed = user_state.last_reminder_failure_at
    if failed and now < failed + timedelta(seconds=retry_interval):
        return False
    return now.astimezone(tz).date() >= next_date(user, user_state, now, tz)


def reminder_message(
    settings: CoreSettings, tz: ZoneInfo, user: User, states: dict[str, SubscriptionState],
    user_state: UserState, now: float, test: bool = False,
) -> dict:
    period = user.reminder or "weekly"
    last = user_state.last_contact()
    current = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(tz)
    subs = []
    for sub in user.subscriptions:
        if not sub.enabled:
            continue
        checked = states[sub.id].last_success_at if sub.id in states else None
        subs.append({
            "id": sub.id,
            "title": sub.title,
            "time": sub.time,
            "transport": sub.transport,
            "widget_url": sub.widget_url,
            "last_check": checked.astimezone(tz).date().isoformat() if checked else None,
        })
    return {
        "type": "reminder",
        "test": test,
        "generated_at": current.isoformat(timespec="seconds"),
        "timezone": tz.key,
        "recipient": {"email": user.email},
        "period": {"name": period, "days": PERIOD_DAYS[period]},
        "last_contact": last.astimezone(tz).date().isoformat() if last else None,
        "days_without_messages": (current.date() - last.astimezone(tz).date()).days if last else None,
        "subscriptions": subs,
        "header": settings.messages.header,
        "footer": settings.messages.footer,
        "links": {"profile": (settings.frontend.public_url + "/") if settings.frontend.public_url else None},
        "no_frontend_text": settings.messages.unsubscribe_text,
    }


class ReminderSender:
    def __init__(
        self, settings: CoreSettings, tz: ZoneInfo, state: StateStore, users: UserStateStore,
        mailer: SystemMailer, metrics: CoreMetrics,
    ) -> None:
        self.settings = settings
        self.tz = tz
        self.state = state
        self.users = users
        self.mailer = mailer
        self.metrics = metrics
        self.env = make_environment(None)

    def states(self, user: User) -> dict[str, SubscriptionState]:
        return {s.id: self.state.get(s.id) for s in user.subscriptions}

    def track_setting(self, user: User, now: datetime) -> UserState:
        """Start the first period when the setting is turned on."""
        state = self.users.get(user.email)
        if state.reminder == user.reminder:
            return state

        def change(s: UserState) -> None:
            if user.reminder and not s.reminder:
                s.since = now
            s.reminder = user.reminder

        return self.users.update(user.email, change)

    def render(self, message: dict) -> tuple[str, str]:
        subject = ("[TEST] " if message["test"] else "") + "Zabbix digest: the service is active"
        try:
            body = self.env.get_template("reminder.html.j2").render(m=message, subject=subject)
        except TemplateError as exc:
            raise RenderError(f"template error: {type(exc).__name__}: {exc}") from None
        return subject, body

    def status(self, user: User) -> dict:
        now = datetime.now(timezone.utc)
        state = self.users.get(user.email)
        day = next_date(user, state, now, self.tz)
        return {
            "reminder": user.reminder,
            "last_contact_at": state.last_contact_at.isoformat(timespec="seconds") if state.last_contact_at else None,
            "next_date": day.isoformat() if day else None,
            "blocked": blocked_reason(user, self.states(user)) if user.reminder else None,
        }

    async def send(self, user: User, test: bool = False) -> None:
        """Raises RenderError or an SMTP error. A test message does not change the state."""
        with log_context(user=user.email):
            message = reminder_message(
                self.settings, self.tz, user, self.states(user), self.users.get(user.email), time.time(), test
            )
            subject, body = self.render(message)
            await self.mailer.send_html(user.email, subject, body, html_to_text(body))

    async def run(self, user: User) -> str:
        """Scheduled reminder. Returns delivered or failed."""
        with log_context(user=user.email):
            try:
                await self.send(user)
            except Exception as exc:  # noqa: BLE001
                now = datetime.now(timezone.utc)
                self.users.update(user.email, lambda s: setattr(s, "last_reminder_failure_at", now))
                self.metrics.operations.labels(operation="reminder", result="error").inc()
                log.warning("reminder not sent: %s", describe_error(exc), extra={"operation": "reminder"})
                return "failed"
            now = datetime.now(timezone.utc)

            def change(s: UserState) -> None:
                s.last_reminder_at = now
                s.last_reminder_failure_at = None
                s.last_contact_at = now

            self.users.update(user.email, change)
            self.metrics.operations.labels(operation="reminder", result="ok").inc()
            log.info("reminder sent (%s)", user.reminder, extra={"operation": "reminder"})
            return "delivered"
