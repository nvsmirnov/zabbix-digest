import asyncio
import json
import logging
import sys
from datetime import date, datetime, timezone

import pytest

from common.yamlio import dumps_yaml
from core.app import CoreApp
from core.errors import PermanentTransportError, TransportError
from core.messages import SCHEMA_VERSION, format_duration
from core.transports.email import EmailAdapter

from core_helpers import RecordingAdapter, make_settings, users_data
from fake_zabbix import TOKEN, FakeZabbix

SLOT = date(2026, 10, 28)


async def make_core(tmp_path, fake=None, users=None, **overrides):
    (tmp_path / "users.yaml").write_text(dumps_yaml(users or users_data()))
    settings = make_settings(tmp_path, **overrides)
    fake = fake or FakeZabbix()
    core = CoreApp(settings, zabbix_transport=fake.transport())
    adapter = RecordingAdapter()
    core.transports.transports["email"].adapter = adapter
    await core.source.start(core.stop)
    return core, fake, adapter


def sub_of(core):
    return core.source.current.find_subscription("a1b2")


async def test_digest_delivered_and_state_recorded(tmp_path):
    core, fake, adapter = await make_core(tmp_path)
    result = await core.processor.run_scheduled(*sub_of(core), SLOT)
    assert result == "delivered"
    msg = adapter.sent[0]
    assert msg["schema_version"] == SCHEMA_VERSION and msg["type"] == "digest"
    assert msg["recipient"] == {"email": "alice@example.com"}
    assert msg["digest"]["shown"] == 2 and msg["digest"]["total"] == 3 and msg["digest"]["truncated"]
    assert TOKEN not in json.dumps(msg)
    state = core.state.get("a1b2")
    assert state.last_completed_slot == "2026-10-28" and state.last_result == "delivered"
    assert state.attempt_started_at is None
    await core.aclose()


async def test_min_severity_narrows_digest(tmp_path):
    core, fake, adapter = await make_core(tmp_path, users=users_data(min_severity=5))
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "delivered"
    d = adapter.sent[0]["digest"]
    assert [p["name"] for p in d["problems"]] == ["Disk full"]
    assert d["filters"] == {"min_severity": {"value": 5, "name": "Disaster"}, "hide_acknowledged": False,
                            "hide_suppressed": False}
    await core.aclose()


async def test_hide_acknowledged_override(tmp_path):
    core, fake, adapter = await make_core(tmp_path, users=users_data(hide_acknowledged=True, hide_suppressed=True))
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "delivered"
    d = adapter.sent[0]["digest"]
    assert "CPU high" not in [p["name"] for p in d["problems"]] and d["total"] == 2
    assert d["filters"]["hide_acknowledged"] and d["filters"]["hide_suppressed"]
    params = [p for m, p, _ in fake.calls if m == "problem.get"][-1]
    assert params["acknowledged"] is False and params["suppressed"] is False
    _, body = EmailAdapter("email", {}, core.settings).render(adapter.sent[0])
    assert "Subscription filters: acknowledged problems hidden; suppressed problems hidden." in body
    await core.aclose()


async def test_min_severity_outside_widget_filter_is_empty(tmp_path):
    fake = FakeZabbix()
    fake.fields = [f for f in fake.fields if f["name"] != "severities.1"]  # widget: High only
    core, _, adapter = await make_core(tmp_path, fake, users=users_data(min_severity="Disaster"))
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "empty"
    assert adapter.sent == [] and not [c for c in fake.calls if c[0] == "problem.get"]
    assert core.state.get("a1b2").last_completed_slot == "2026-10-28"
    await core.aclose()


async def test_empty_report_not_sent(tmp_path):
    fake = FakeZabbix()
    fake.problems = []
    core, _, adapter = await make_core(tmp_path, fake)
    state = core.state.get("a1b2")
    state.consecutive_failures = 3
    core.state.save(state)
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "empty"
    assert adapter.sent == []
    state = core.state.get("a1b2")
    assert state.last_result == "empty" and state.consecutive_failures == 0 and state.last_completed_slot == "2026-10-28"
    await core.aclose()


async def test_invalid_token_sends_error_notification(tmp_path):
    users = users_data()
    users["users"][0]["zabbix_token"] = "z" * 64
    core, _, adapter = await make_core(tmp_path, users=users)
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "error_notified"
    msg = adapter.sent[0]
    assert msg["type"] == "error" and msg["error"]["category"] == "zabbix_auth"
    assert "z" * 64 not in json.dumps(msg)
    state = core.state.get("a1b2")
    assert state.last_result == "error_notified" and state.consecutive_failures == 1
    assert state.last_completed_slot == "2026-10-28"
    # Subscription stays enabled.
    assert sub_of(core)[1].enabled
    await core.aclose()


async def test_zabbix_down_sends_error_notification(tmp_path):
    fake = FakeZabbix()
    fake.down = True
    core, _, adapter = await make_core(tmp_path, fake)
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "error_notified"
    assert adapter.sent[0]["error"]["category"] == "zabbix_unavailable"
    assert core.metrics.zabbix_retries._value.get() >= 1
    await core.aclose()


async def test_delivery_failure_retried_later(tmp_path, caplog):
    core, _, adapter = await make_core(tmp_path)
    adapter.fail = [TransportError("down"), TransportError("down")]
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "failed"
    # Every attempt is logged, then the final failure.
    messages = [r.getMessage() for r in caplog.records if r.name == "core.transports.base"]
    assert messages == [
        "delivery attempt 1/2 failed, retrying in 0.0s: down",
        "delivery attempt 2/2 failed: down",
        "delivery failed after 2 attempts, giving up",
    ]
    state = core.state.get("a1b2")
    assert state.last_result == "failed" and state.last_completed_slot is None and state.consecutive_failures == 1
    assert len(adapter.sent) == 0
    # Not due again before the retry interval, due afterwards (fresh preparation).
    state.last_failure_at = datetime(2026, 10, 28, 8, 5, tzinfo=timezone.utc)
    core.state.save(state)
    assert core.scheduler.due(datetime(2026, 10, 28, 8, 10, tzinfo=timezone.utc)) == []
    assert len(core.scheduler.due(datetime(2026, 10, 28, 8, 16, tzinfo=timezone.utc))) == 1
    await core.aclose()


async def test_delivery_retry_within_attempts(tmp_path):
    core, _, adapter = await make_core(tmp_path)
    adapter.fail = [TransportError("busy")]
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "delivered"
    assert core.metrics.transport_retries.labels(transport="email")._value.get() == 1
    await core.aclose()


async def test_permanent_delivery_error_not_retried(tmp_path):
    core, _, adapter = await make_core(tmp_path)
    adapter.fail = [PermanentTransportError("rejected"), TransportError("x")]
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "failed"
    assert len(adapter.fail) == 1
    await core.aclose()


async def test_scheduler_tick_and_disabled(tmp_path):
    users = users_data()
    users["users"][0]["subscriptions"].append(
        {"id": "c3d4", "widget_url": users["users"][0]["subscriptions"][0]["widget_url"], "time": "08:00", "enabled": False}
    )
    core, _, adapter = await make_core(tmp_path, users=users)
    now = datetime(2026, 10, 28, 8, 30, tzinfo=timezone.utc)
    tasks = core.scheduler.tick(now)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    assert len(adapter.sent) == 1
    assert core.scheduler.tick(now) == []
    await core.aclose()


async def test_preview_and_test_send_do_not_touch_state(tmp_path):
    core, _, adapter = await make_core(tmp_path)
    core.transports.transports["email"].adapter = EmailAdapter("email", {}, core.settings)
    result = await core.processor.preview(*sub_of(core))
    assert "Memory low" in result["body"] and "Disk full" not in result["body"]
    assert "Showing 2 of 3" in result["body"]
    assert not (tmp_path / "state" / "a1b2.yaml").exists()
    rec = RecordingAdapter()
    core.transports.transports["email"].adapter = rec
    await core.processor.test_send(*sub_of(core))
    assert rec.sent[0]["type"] == "test"
    assert not (tmp_path / "state" / "a1b2.yaml").exists()
    await core.aclose()


async def test_state_cleanup_on_removal(tmp_path):
    core, _, _ = await make_core(tmp_path)
    await core.processor.run_scheduled(*sub_of(core), SLOT)
    assert (tmp_path / "state" / "a1b2.yaml").exists()
    users = users_data()
    users["users"][0]["subscriptions"] = []
    users["revision"] = 2
    await core.aclose()
    # Restart with the subscription removed: startup cleanup deletes its state file.
    restarted, _, _ = await make_core(tmp_path, users=users)
    restarted.scheduler.cleanup()
    assert not (tmp_path / "state" / "a1b2.yaml").exists()
    await restarted.aclose()


async def test_once_force_sends_now_without_state(tmp_path):
    users = users_data()
    users["users"][0]["subscriptions"].append(
        {"id": "c3d4", "widget_url": users["users"][0]["subscriptions"][0]["widget_url"], "time": "08:00", "enabled": False}
    )
    users["users"][0]["subscriptions"][0]["time"] = "23:59"  # not due during the test
    core, _, adapter = await make_core(tmp_path, users=users)
    assert core.scheduler.due(datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)) == []
    result = await core.run_once(force=True)
    assert result == {"a1b2": "delivered"}
    assert [m["subscription"]["id"] for m in adapter.sent] == ["a1b2"]
    assert not (tmp_path / "state" / "a1b2.yaml").exists()
    assert await core.run_once({"c3d4"}, force=True) == {}
    await core.aclose()


async def test_once_force_sends_error_notification(tmp_path):
    users = users_data()
    users["users"][0]["zabbix_token"] = "z" * 64
    core, _, adapter = await make_core(tmp_path, users=users)
    assert await core.run_once(force=True) == {"a1b2": "error_notified"}
    assert adapter.sent[0]["type"] == "error"
    assert not (tmp_path / "state" / "a1b2.yaml").exists()
    await core.aclose()


async def test_once_without_force_reports_results(tmp_path):
    core, _, _ = await make_core(tmp_path)
    tasks = core.scheduler.tick(datetime(2026, 10, 28, 8, 30, tzinfo=timezone.utc))
    assert await asyncio.gather(*tasks) == ["delivered"]
    await core.aclose()


async def test_file_mode_reads_users_only_at_startup(tmp_path):
    core, _, _ = await make_core(tmp_path)
    users = users_data()
    users["revision"] = 2
    (tmp_path / "users.yaml").write_text(dumps_yaml(users))
    assert not await core.source.refresh()
    assert core.source.current.revision == 1
    await core.aclose()


async def test_shutdown_waits_for_delivery(tmp_path):
    core, _, _ = await make_core(tmp_path)

    class Slow(RecordingAdapter):
        async def send(self, message):
            await asyncio.sleep(0.3)
            self.sent.append(message)

    slow = Slow()
    core.transports.transports["email"].adapter = slow
    tasks = core.scheduler.tick(datetime(2026, 10, 28, 8, 30, tzinfo=timezone.utc))
    for _ in range(100):
        if core.processor.delivering:
            break
        await asyncio.sleep(0.01)
    await core.scheduler.shutdown(timeout=5)
    assert len(slow.sent) == 1
    assert core.state.get("a1b2").last_result == "delivered"
    assert all(t.done() for t in tasks)
    await core.aclose()


async def test_email_rendering_escapes_untrusted_values(tmp_path):
    fake = FakeZabbix()
    fake.problems[2]["name"] = "<script>alert(1)</script>"
    users = users_data(name="<b>x</b>")
    core, _, _ = await make_core(tmp_path, fake, users=users)
    adapter = EmailAdapter("email", {}, core.settings)
    core.transports.transports["email"].adapter = adapter
    msg = await core.processor.prepare_digest(*sub_of(core))
    _, body = adapter.render(msg)
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "<b>x</b>" not in body
    email = adapter.build(msg)
    assert email["To"] == "alice@example.com"
    assert "Zabbix digest: <b>x</b>" in email["Subject"]
    await core.aclose()


async def test_custom_template_override(tmp_path):
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "test.html.j2").write_text("CUSTOM {{ m.subscription.id }}")
    core, _, _ = await make_core(tmp_path)
    adapter = EmailAdapter("email", {"template_dir": str(tdir)}, core.settings)
    from core.messages import test_message

    user, sub = sub_of(core)
    assert adapter.render(test_message(core.settings, core.tz, user, sub, 0))[1] == "CUSTOM a1b2"
    # Digest falls back to the built-in template.
    msg = await core.processor.prepare_digest(user, sub)
    assert "<table" in adapter.render(msg)[1]
    await core.aclose()


def test_format_duration():
    assert format_duration(5) == "5s"
    assert format_duration(125) == "2m 5s"
    assert format_duration(3 * 3600 + 120) == "3h 2m"
    assert format_duration(90000) == "1d 1h 0m"


@pytest.mark.parametrize("code, outcome", [(0, "delivered"), (75, "failed"), (3, "failed")])
async def test_external_adapter(tmp_path, code, outcome):
    script = tmp_path / "sender.py"
    out = tmp_path / "out.json"
    script.write_text(
        "import sys, json\n"
        f"data = json.load(sys.stdin)\nopen({str(out)!r}, 'w').write(json.dumps(data))\nsys.exit({code})\n"
    )
    transports = {
        "email": {"type": "email"},
        "custom": {
            "type": "external",
            "send_timeout": "10s",
            "retry": {"attempts": 2, "initial_delay": 0},
            "settings": {"command": [sys.executable, str(script)]},
        },
    }
    (tmp_path / "users.yaml").write_text(dumps_yaml(users_data(transport="custom")))
    settings = make_settings(tmp_path, transports=transports)
    core = CoreApp(settings, zabbix_transport=FakeZabbix().transport())
    await core.source.start(core.stop)
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == outcome
    data = json.loads(out.read_text())
    assert data["type"] == "digest" and data["subscription"]["id"] == "a1b2"
    attempts = core.metrics.delivery_attempt_failures.labels(transport="custom")._value.get()
    assert attempts == {0: 0, 75: 2, 3: 1}[code]
    await core.aclose()


async def test_external_adapter_timeout(tmp_path):
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(30)\n")
    transports = {
        "email": {"type": "email"},
        "custom": {
            "type": "external",
            "send_timeout": "0.5s",
            "retry": {"attempts": 1},
            "settings": {"command": [sys.executable, str(script)]},
        },
    }
    (tmp_path / "users.yaml").write_text(dumps_yaml(users_data(transport="custom")))
    core = CoreApp(make_settings(tmp_path, transports=transports), zabbix_transport=FakeZabbix().transport())
    await core.source.start(core.stop)
    user, sub = sub_of(core)
    from core.transports.base import DeliveryFailed

    with pytest.raises(DeliveryFailed) as info:
        await core.processor.test_send(user, sub)
    assert "timed out" in info.value.reason
    await core.aclose()


async def test_digest_layout_problems_first(tmp_path):
    from core.transports.email import html_to_text

    core, _, adapter = await make_core(tmp_path)
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "delivered"
    email = EmailAdapter("email", {}, core.settings)
    _, body = email.render(adapter.sent[0])
    # The table comes before the summary; the summary uses the new wording.
    assert body.index("<table") < body.index("Total 3 active problems") < body.index('widget "DB problems"')
    assert 'Source: <a href="' in body and 'on dashboard "Ops"' in body
    text = html_to_text(body)
    first = text.splitlines()[0]
    # Plain text part (used by notification previews) starts with the first problem row.
    assert first.split(" | ")[2] == "app2" and "Memory low" in first
    assert "Zabbix digest" not in first
    await core.aclose()


async def test_test_message_layout_like_digest(tmp_path):
    from core.messages import test_message
    from core.transports.email import html_to_text

    core, _, _ = await make_core(tmp_path)
    user, sub = sub_of(core)
    email = EmailAdapter("email", {}, core.settings)
    _, body = email.render(test_message(core.settings, core.tz, user, sub, 1_800_000_000))
    # Banner on top, then the table, then the same summary as in digests.
    assert body.index("TEST MESSAGE") < body.index("<table") < body.index("Total 3 active problems")
    assert body.index("Total 3 active problems") < body.index("Generated at") < body.index('widget "TEST"')
    assert "<h1" not in body and "Operational data" not in body
    lines = html_to_text(body).splitlines()
    assert lines[0].startswith("TEST MESSAGE") and lines[1].split(" | ")[2] == "test-host-1"
    await core.aclose()


@pytest.fixture
def ctx_records():
    """Records as the production handler sees them (context fields added)."""
    from common.logs import _ContextDefaults

    records = []

    class Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Handler(level=logging.INFO)
    handler.addFilter(_ContextDefaults("core"))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield records
    root.removeHandler(handler)
    root.setLevel(level)


async def test_logs_name_user_and_subscription(tmp_path, ctx_records):
    from common.logs import TextFormatter

    core, _, adapter = await make_core(tmp_path)
    adapter.fail = [TransportError("connection refused")]
    assert await core.processor.run_scheduled(*sub_of(core), SLOT) == "delivered"
    by_message = {r.getMessage(): r for r in ctx_records}
    attempt = next(r for m, r in by_message.items() if m.startswith("delivery attempt 1/"))
    delivered = next(r for m, r in by_message.items() if m.startswith("digest delivered"))
    for record in (attempt, delivered):
        assert record.user == "alice@example.com" and record.subscription_id == "a1b2"
    assert "user=alice@example.com subscription_id=a1b2" in TextFormatter().format(delivered)
    # The context ends with the processing.
    logging.getLogger("core.test").info("outside")
    assert getattr(ctx_records[-1], "user", None) is None
    await core.aclose()
