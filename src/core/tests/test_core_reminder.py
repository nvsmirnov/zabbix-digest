from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from common.yamlio import dumps_yaml
from core.api import create_app
from core.app import CoreApp
from core.reminder import blocked_reason, day_finished, reminder_due, next_date
from core.source import API_KEY_HEADER
from core.state import RESULT_DELIVERED, RESULT_EMPTY, SubscriptionState
from core.transports.email import html_to_text
from core.user_state import UserState, UserStateStore, user_key
from core.users import User

from core_helpers import RecordingAdapter, make_settings, users_data
from fake_zabbix import TOKEN, WIDGET_URL, FakeZabbix

UTC = ZoneInfo("UTC")
WINDOW = 7200
RETRY = 600
KEY = "k" * 32


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def make_user(period="weekly", subs=(("a1", "08:00", True), ("b2", "18:00", True))) -> User:
    return User(
        email="alice@example.com", zabbix_token=TOKEN, reminder=period,
        subscriptions=[{"id": i, "widget_url": WIDGET_URL, "time": t, "enabled": e} for i, t, e in subs],
    )


def done(day: str, result=RESULT_EMPTY, **kw) -> SubscriptionState:
    return SubscriptionState(subscription_id="x", last_completed_slot=day, last_result=result, **kw)


def states_done(day="2026-10-28", result=RESULT_EMPTY) -> dict:
    return {"a1": done(day, result), "b2": done(day, result)}


def test_due_after_last_subscription_of_the_day_and_period():
    user = make_user()
    us = UserState(email=user.email, since=at("2026-10-21T09:00"))
    # 7 days after the baseline, after both subscriptions (08:00, 18:00) finished today.
    assert reminder_due(user, states_done(), us, at("2026-10-28T18:05"), UTC, WINDOW, RETRY)
    # Before the 18:00 subscription of the day: not yet.
    states = {"a1": done("2026-10-28"), "b2": done("2026-10-27")}
    assert not reminder_due(user, states, us, at("2026-10-28T12:00"), UTC, WINDOW, RETRY)
    # 18:00 slot not completed but its window ended: the day is over.
    assert day_finished(user, states, at("2026-10-28T20:00"), UTC, WINDOW)
    # Only 6 days.
    us = UserState(email=user.email, since=at("2026-10-22T09:00"))
    assert not reminder_due(user, states_done(), us, at("2026-10-28T18:05"), UTC, WINDOW, RETRY)


def test_monthly_period_and_last_contact():
    user = make_user("monthly")
    us = UserState(email=user.email, since=at("2026-08-01T09:00"), last_contact_at=at("2026-09-29T08:00"))
    assert next_date(user, us, at("2026-10-28T18:05"), UTC) == date(2026, 10, 29)
    assert not reminder_due(user, states_done(), us, at("2026-10-28T18:05"), UTC, WINDOW, RETRY)
    assert reminder_due(user, states_done("2026-10-29"), us, at("2026-10-29T18:05"), UTC, WINDOW, RETRY)


def test_blocked_by_errors_missing_checks_and_no_subscriptions():
    user = make_user()
    us = UserState(email=user.email, since=at("2026-10-01T09:00"))
    states = {"a1": done("2026-10-28"), "b2": done("2026-10-28", "error_notified")}
    assert blocked_reason(user, states) == "errors"
    assert not reminder_due(user, states, us, at("2026-10-28T18:05"), UTC, WINDOW, RETRY)
    states = {"a1": done("2026-10-28"), "b2": SubscriptionState(subscription_id="b2")}
    assert blocked_reason(user, states) == "not_checked"
    off = make_user(subs=(("a1", "08:00", False),))
    assert blocked_reason(off, {"a1": done("2026-10-28")}) == "no_subscriptions"
    # Disabled subscriptions are ignored.
    mixed = make_user(subs=(("a1", "08:00", True), ("b2", "18:00", False)))
    assert blocked_reason(mixed, {"a1": done("2026-10-28"), "b2": done("2026-10-01", "failed")}) is None


def test_off_and_retry_interval():
    us = UserState(email="alice@example.com", since=at("2026-10-01T09:00"))
    assert not reminder_due(make_user(None), states_done(), us, at("2026-10-28T18:05"), UTC, WINDOW, RETRY)
    us.last_reminder_failure_at = at("2026-10-28T18:01")
    assert not reminder_due(make_user(), states_done(), us, at("2026-10-28T18:05"), UTC, WINDOW, RETRY)
    assert reminder_due(make_user(), states_done(), us, at("2026-10-28T18:12"), UTC, WINDOW, RETRY)


def test_user_state_store(tmp_path):
    store = UserStateStore(tmp_path / "users")
    store.record_contact("Alice@Example.com", at("2026-10-28T08:00"))
    store.record_contact("alice@example.com", at("2026-10-27T08:00"))
    assert store.get("alice@example.com").last_contact_at == at("2026-10-28T08:00")
    # The file name does not contain the email.
    assert [p.name for p in (tmp_path / "users").iterdir()] == [f"{user_key('alice@example.com')}.yaml"]
    assert store.cleanup({"bob@example.com"}) == [user_key("alice@example.com")]


async def make_core(tmp_path, **user_fields):
    data = users_data()
    data["users"][0].update(user_fields)
    (tmp_path / "users.yaml").write_text(dumps_yaml(data))
    core = CoreApp(make_settings(tmp_path, internal_api_key=KEY, frontend={"public_url": "https://digest.example.com"}),
                   zabbix_transport=FakeZabbix().transport())
    await core.source.start(core.stop)
    core.transports.transports["email"].adapter = RecordingAdapter()
    sent = []

    async def send_html(to, subject, body, text):
        sent.append((to, subject, body, text))

    core.mailer.send_html = send_html
    return core, sent


async def test_digest_and_error_notification_are_contacts(tmp_path):
    core, _ = await make_core(tmp_path)
    user, sub = core.source.current.find_subscription("a1b2")
    await core.processor.test_send(user, sub)
    assert core.user_state.get(user.email).last_contact_at is None
    assert await core.processor.run_scheduled(user, sub, date(2026, 10, 28)) == "delivered"
    assert core.user_state.get(user.email).last_contact_at is not None
    await core.aclose()


async def test_scheduler_sends_reminder(tmp_path):
    core, sent = await make_core(tmp_path, reminder="weekly")
    user = core.source.current.find_user("alice@example.com")
    now = datetime.now(timezone.utc)
    # First tick: the setting is seen, the first period starts now. Nothing is sent.
    core.scheduler.tick_reminders(now)
    assert core.user_state.get(user.email).since is not None and not sent
    # Today's slot checked with no problems; 8 days since the setting was turned on.
    today = now.date()
    state = core.state.get("a1b2")
    state.record_success(today, now, RESULT_EMPTY)
    core.state.save(state)
    core.user_state.update(user.email, lambda s: setattr(s, "since", now - timedelta(days=8)))
    later = max(now, datetime.combine(today, datetime.min.time(), timezone.utc) + timedelta(hours=8, minutes=1))
    for task in core.scheduler.tick_reminders(later):
        assert await task == "delivered"
    assert len(sent) == 1
    to, subject, body, text = sent[0]
    assert to == "alice@example.com" and subject == "Zabbix digest: the service is active"
    assert "No digests were sent to you for 8 days" in text and "DB (#a1b2)" in text
    assert "turn off" in text and "delete your profile" in text and "https://digest.example.com/" in body
    # Sent: counts as a contact, nothing more for a week.
    assert core.user_state.get(user.email).last_reminder_at is not None
    assert core.scheduler.tick_reminders(later) == []
    await core.aclose()


async def test_reminder_endpoints(tmp_path):
    core, sent = await make_core(tmp_path, reminder="monthly")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(core)), base_url="http://core")
    h = {API_KEY_HEADER: KEY}
    r = await client.post("/internal/reminder/status", headers=h, json={"email": "Alice@example.com"})
    assert r.status_code == 200
    assert r.json()["reminder"] == "monthly" and r.json()["blocked"] == "not_checked"
    r = await client.post("/internal/reminder/test", headers=h, json={"email": "alice@example.com"})
    assert r.status_code == 200 and sent[0][1] == "[TEST] Zabbix digest: the service is active"
    assert html_to_text(sent[0][2]).startswith("TEST MESSAGE")
    # A test does not change the state.
    assert core.user_state.get("alice@example.com").last_reminder_at is None
    r = await client.post("/internal/reminder/status", headers=h, json={"email": "nobody@example.com"})
    assert r.status_code == 409 and r.json()["error"] == "unknown_user"
    await core.aclose()


def test_old_field_name_accepted():
    user = User(email="alice@example.com", zabbix_token=TOKEN, liveness_report="monthly")
    assert user.reminder == "monthly"
