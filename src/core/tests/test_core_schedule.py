from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.schedule import apply_time_change, due_slot, slot_start
from core.state import RESULT_DELIVERED, StateStore, SubscriptionState

UTC = ZoneInfo("UTC")
BERLIN = ZoneInfo("Europe/Berlin")
WINDOW = 7200
RETRY = 600


def at(text: str, tz=UTC) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=tz)


def st(**kw) -> SubscriptionState:
    return SubscriptionState(subscription_id="a1b2", **kw)


def test_before_inside_after_window():
    assert due_slot("08:00", st(), at("2026-10-28T07:59"), UTC, WINDOW, RETRY) is None
    assert due_slot("08:00", st(), at("2026-10-28T08:00"), UTC, WINDOW, RETRY) == date(2026, 10, 28)
    assert due_slot("08:00", st(), at("2026-10-28T09:59"), UTC, WINDOW, RETRY) == date(2026, 10, 28)
    assert due_slot("08:00", st(), at("2026-10-28T10:00"), UTC, WINDOW, RETRY) is None


def test_completed_slot_not_repeated_after_time_change():
    state = st(last_completed_slot="2026-10-28")
    # Subscription moved from 08:00 to 11:00 after today's execution.
    assert due_slot("11:00", state, at("2026-10-28T11:30"), UTC, WINDOW, RETRY) is None
    assert due_slot("11:00", state, at("2026-10-29T11:30"), UTC, WINDOW, RETRY) == date(2026, 10, 29)



def test_time_moved_later_today_reopens_slot():
    # Delivered at 08:00, at 10:00 the time is changed to 15:00: sent again today at 15:00.
    state = st(last_completed_slot="2026-10-28")
    assert apply_time_change(state, "15:00", at("2026-10-28T10:00"), UTC)
    assert state.reopened_slot == "2026-10-28"
    assert due_slot("15:00", state, at("2026-10-28T14:59"), UTC, WINDOW, RETRY) is None
    assert due_slot("15:00", state, at("2026-10-28T15:00"), UTC, WINDOW, RETRY) == date(2026, 10, 28)
    state.record_success(date(2026, 10, 28), at("2026-10-28T15:01"), RESULT_DELIVERED)
    assert state.reopened_slot is None
    assert due_slot("15:00", state, at("2026-10-28T15:30"), UTC, WINDOW, RETRY) is None


def test_time_moved_to_passed_moment_does_not_repeat():
    state = st(last_completed_slot="2026-10-28")
    assert not apply_time_change(state, "09:00", at("2026-10-28T10:00"), UTC)
    assert due_slot("09:00", state, at("2026-10-28T10:00"), UTC, WINDOW, RETRY) is None


def test_reopened_slot_closed_when_moved_to_passed_moment():
    state = st(last_completed_slot="2026-10-28")
    assert apply_time_change(state, "15:00", at("2026-10-28T10:00"), UTC)
    # Moved again, still ahead: stays reopened.
    assert not apply_time_change(state, "16:00", at("2026-10-28T10:05"), UTC)
    assert state.reopened_slot == "2026-10-28"
    # Moved to a passed moment: no immediate second digest.
    assert apply_time_change(state, "09:30", at("2026-10-28T10:10"), UTC)
    assert state.last_completed_slot == "2026-10-28" and state.reopened_slot is None
    assert due_slot("09:30", state, at("2026-10-28T10:10"), UTC, WINDOW, RETRY) is None


def test_time_change_without_completed_slot_changes_nothing():
    state = st(last_completed_slot="2026-10-27")
    assert not apply_time_change(state, "15:00", at("2026-10-28T10:00"), UTC)
    assert state.last_completed_slot == "2026-10-27" and state.reopened_slot is None


def test_time_change_uses_core_time_zone():
    # The new time is compared in the Core time zone: 23:00 has passed at 23:30 Berlin, not at 22:00.
    state = st(last_completed_slot="2026-10-28")
    assert not apply_time_change(state, "23:00", at("2026-10-28T23:30", BERLIN).astimezone(timezone.utc), BERLIN)
    state = st(last_completed_slot="2026-10-28")
    assert apply_time_change(state, "23:00", at("2026-10-28T22:00", BERLIN), BERLIN)

def test_window_crosses_midnight():
    assert due_slot("23:30", st(), at("2026-10-29T00:45"), UTC, WINDOW, RETRY) == date(2026, 10, 28)
    assert due_slot("23:30", st(last_completed_slot="2026-10-28"), at("2026-10-29T00:45"), UTC, WINDOW, RETRY) is None
    assert due_slot("23:30", st(), at("2026-10-29T01:31"), UTC, WINDOW, RETRY) is None


def test_retry_interval_after_failure():
    failed = at("2026-10-28T08:05")
    state = st(last_failure_at=failed, last_result="failed")
    assert due_slot("08:00", state, at("2026-10-28T08:10"), UTC, WINDOW, RETRY) is None
    assert due_slot("08:00", state, at("2026-10-28T08:15"), UTC, WINDOW, RETRY) == date(2026, 10, 28)
    # Failure of yesterday does not delay today.
    state = st(last_failure_at=at("2026-10-27T08:05"))
    assert due_slot("08:00", state, at("2026-10-28T08:01"), UTC, WINDOW, RETRY) == date(2026, 10, 28)


def test_dst_nonexistent_time_shifted_forward():
    # 2026-03-29 02:30 does not exist in Berlin; it runs at 03:30 CEST (01:30 UTC).
    start = slot_start(date(2026, 3, 29), "02:30", BERLIN)
    assert start == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)


def test_dst_ambiguous_time_first_occurrence():
    # 2026-10-25 02:30 happens twice in Berlin; the first (CEST, UTC+2) is used.
    start = slot_start(date(2026, 10, 25), "02:30", BERLIN)
    assert start == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    state = st()
    assert due_slot("02:30", state, start + timedelta(hours=1), BERLIN, WINDOW, RETRY) == date(2026, 10, 25)
    state.last_completed_slot = "2026-10-25"
    assert due_slot("02:30", state, start + timedelta(hours=1, minutes=30), BERLIN, WINDOW, RETRY) is None


def test_state_store_roundtrip_and_cleanup(tmp_path):
    store = StateStore(str(tmp_path))
    state = store.get("a1b2")
    state.record_success(date(2026, 10, 28), at("2026-10-28T08:01"), RESULT_DELIVERED)
    store.save(state)
    (tmp_path / "notes.txt").write_text("keep")
    (tmp_path / "ffff.yaml").write_text("subscription_id: ffff\n")
    fresh = StateStore(str(tmp_path)).get("a1b2")
    assert fresh.last_completed_slot == "2026-10-28"
    assert fresh.last_success_at == at("2026-10-28T08:01")
    assert fresh.consecutive_failures == 0
    removed = store.cleanup(keep={"a1b2"}, busy=set())
    assert removed == ["ffff"]
    assert (tmp_path / "notes.txt").exists()
    assert oct((tmp_path / "a1b2.yaml").stat().st_mode & 0o777) == "0o600"


def test_failure_transitions():
    state = st()
    state.record_failure(date(2026, 10, 28), at("2026-10-28T08:01"), "delivery", completed=False)
    state.record_failure(date(2026, 10, 28), at("2026-10-28T08:11"), "zabbix_auth", completed=True)
    assert state.consecutive_failures == 2
    assert state.last_completed_slot == "2026-10-28"
    assert state.last_result == "error_notified"
    state.record_success(date(2026, 10, 29), at("2026-10-29T08:01"), "empty")
    assert state.consecutive_failures == 0
