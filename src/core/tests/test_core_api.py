import asyncio
import socket

import httpx
import pytest
from aiosmtpd.controller import Controller

from common.yamlio import dumps_yaml, load_yaml
from core.api import create_app
from core.app import CoreApp
from core.source import API_KEY_HEADER, HttpSource

from core_helpers import make_settings, users_data
from fake_zabbix import OTHER_TOKEN, TOKEN, FakeZabbix

KEY = "k" * 32


async def api_core(tmp_path, fake=None, **overrides):
    (tmp_path / "users.yaml").write_text(dumps_yaml(users_data()))
    fake = fake or FakeZabbix()
    core = CoreApp(make_settings(tmp_path, internal_api_key=KEY, **overrides), zabbix_transport=fake.transport())
    await core.source.start(core.stop)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(core)), base_url="http://core")
    return core, client


async def test_health_and_metrics(tmp_path):
    core, client = await api_core(tmp_path)
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/health/ready")).status_code == 503
    core._scheduler_started = True
    assert (await client.get("/health/ready")).status_code == 200
    metrics = (await client.get("/metrics")).text
    assert "zabbix_digest_core_config_revision 1.0" in metrics
    assert "alice" not in metrics
    await core.aclose()


async def test_internal_requires_key(tmp_path):
    core, client = await api_core(tmp_path)
    assert (await client.get("/internal/info")).status_code == 401
    assert (await client.get("/internal/info", headers={API_KEY_HEADER: "wrong"})).status_code == 401
    info = (await client.get("/internal/info", headers={API_KEY_HEADER: KEY})).json()
    assert info["timezone"] == "UTC" and info["transports"][0]["preview"] is True
    await core.aclose()


async def test_validate_endpoint(tmp_path):
    core, client = await api_core(tmp_path)
    h = {API_KEY_HEADER: KEY}
    assert (await client.post("/internal/config/validate", json=users_data(), headers=h)).json() == {"valid": True}
    bad = users_data(transport="nope")
    response = await client.post("/internal/config/validate", json=bad, headers=h)
    assert response.status_code == 422 and "unknown transport" in response.json()["errors"][0]
    # Validation does not apply the configuration.
    assert core.source.current.users[0].subscriptions[0].transport == "email"
    await core.aclose()


async def test_identify_preview_test_send(tmp_path):
    core, client = await api_core(tmp_path)
    h = {API_KEY_HEADER: KEY}
    r = await client.post("/internal/zabbix/identify", json={"token": OTHER_TOKEN}, headers=h)
    assert r.json()["emails"] == ["bob@example.com", "b2@example.com"]
    r = await client.post("/internal/zabbix/identify", json={"token": "x" * 40}, headers=h)
    assert r.status_code == 422 and r.json()["error"] == "invalid_token"
    body = {"user": {"email": "alice@example.com", "zabbix_token": TOKEN}, "subscription": users_data()["users"][0]["subscriptions"][0]}
    r = await client.post("/internal/preview", json=body, headers=h)
    assert r.status_code == 200 and "Memory low" in r.json()["body"]
    body["user"]["email"] = "bob@example.com"
    r = await client.post("/internal/preview", json=body, headers=h)
    assert r.status_code == 422 and r.json()["error"] == "email_not_owned"
    sent = []

    class Rec:
        supports_preview = True

        async def send(self, message):
            sent.append(message)

    core.transports.transports["email"].adapter = Rec()
    r = await client.post("/internal/test-send", json=body, headers=h)
    assert r.status_code == 200 and sent[0]["type"] == "test"
    await core.aclose()


async def test_send_now(tmp_path):
    fake = FakeZabbix()
    core, client = await api_core(tmp_path, fake)
    h = {API_KEY_HEADER: KEY}
    sent = []

    class Rec:
        supports_preview = True

        async def send(self, message):
            sent.append(message)

    core.transports.transports["email"].adapter = Rec()
    body = {"user": {"email": "alice@example.com", "zabbix_token": TOKEN}, "subscription": users_data()["users"][0]["subscriptions"][0]}
    r = await client.post("/internal/send-now", json=body, headers=h)
    assert r.status_code == 200 and r.json() == {"result": "delivered", "shown": 2, "total": 3, "total_exact": True}
    assert sent[-1]["type"] == "digest"
    # The schedule state is not changed.
    assert core.state.get("a1b2").last_completed_slot is None

    # Preparation errors are returned, no error notification is sent.
    bad = {**body, "user": {"email": "bob@example.com", "zabbix_token": TOKEN}}
    r = await client.post("/internal/send-now", json=bad, headers=h)
    assert r.status_code == 422 and r.json()["error"] == "email_not_owned" and len(sent) == 1

    fake.problems = []
    r = await client.post("/internal/send-now", json=body, headers=h)
    assert r.status_code == 200 and r.json()["result"] == "empty" and len(sent) == 1
    await core.aclose()


def frontend_transport(state: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if state.get("down"):
            raise httpx.ConnectError("down")
        assert request.headers[API_KEY_HEADER] == KEY
        return httpx.Response(200, json=state["config"])

    return httpx.MockTransport(handler)


async def no_sleep(_):
    await asyncio.sleep(0)


async def test_http_source_cache_and_recovery(tmp_path):
    settings = make_settings(
        tmp_path,
        internal_api_key=KEY,
        users={"source": "http", "cache_file": str(tmp_path / "cache.yaml"), "startup_retry": {"attempts": 2, "initial_delay": 0}},
        frontend={"url": "http://web"},
    )
    remote = {"config": users_data()}
    from core.metrics import CoreMetrics

    src = HttpSource(settings, ["email"], CoreMetrics(), transport=frontend_transport(remote), sleep=no_sleep)
    assert await src.start(asyncio.Event())
    assert src.current.revision == 1
    assert load_yaml(tmp_path / "cache.yaml")["revision"] == 1
    assert oct((tmp_path / "cache.yaml").stat().st_mode & 0o777) == "0o600"

    # Invalid remote configuration never replaces the valid one or the cache.
    remote["config"] = {"revision": 2, "users": [{"email": "broken"}]}
    assert not await src.refresh()
    assert src.current.revision == 1 and load_yaml(tmp_path / "cache.yaml")["revision"] == 1

    # Restart with frontend down: the cache is used.
    remote["down"] = True
    src2 = HttpSource(settings, ["email"], CoreMetrics(), transport=frontend_transport(remote), sleep=no_sleep)
    assert await src2.start(asyncio.Event())
    assert src2.current.revision == 1

    # Frontend back with a new revision.
    remote["down"] = False
    new = users_data()
    new["revision"] = 5
    remote["config"] = new
    assert await src2.refresh()
    assert src2.current.revision == 5 and load_yaml(tmp_path / "cache.yaml")["revision"] == 5


async def test_http_source_waits_without_cache(tmp_path):
    settings = make_settings(
        tmp_path,
        internal_api_key=KEY,
        users={"source": "http", "cache_file": str(tmp_path / "none.yaml"), "startup_retry": {"attempts": 1, "initial_delay": 0}},
        frontend={"url": "http://web"},
    )
    remote = {"down": True, "config": users_data()}
    from core.metrics import CoreMetrics

    calls = {"n": 0}

    async def counting_sleep(_):
        calls["n"] += 1
        if calls["n"] == 3:
            remote["down"] = False
        await asyncio.sleep(0)

    src = HttpSource(settings, ["email"], CoreMetrics(), transport=frontend_transport(remote), sleep=counting_sleep)
    assert await asyncio.wait_for(src.start(asyncio.Event()), timeout=5)
    assert src.current is not None and calls["n"] >= 3


@pytest.fixture
def smtp_server():
    messages = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            messages.append(envelope)
            return "250 OK"

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    controller = Controller(Handler(), hostname="127.0.0.1", port=port)
    controller.start()
    yield controller, messages
    controller.stop()


async def test_smtp_delivery(tmp_path, smtp_server):
    controller, messages = smtp_server
    (tmp_path / "users.yaml").write_text(dumps_yaml(users_data()))
    port = controller.port
    settings = make_settings(tmp_path, smtp={"host": "127.0.0.1", "port": port, "from_address": "digest@example.com"})
    core = CoreApp(settings, zabbix_transport=FakeZabbix().transport())
    await core.source.start(core.stop)
    user, sub = core.source.current.find_subscription("a1b2")
    await core.processor.test_send(user, sub)
    assert len(messages) == 1
    raw = messages[0].content.decode()
    assert messages[0].rcpt_tos == ["alice@example.com"]
    assert "[TEST]" in raw and "TEST MESSAGE" in raw
    await core.aclose()


async def test_smtp_unavailable_is_failure(tmp_path):
    (tmp_path / "users.yaml").write_text(dumps_yaml(users_data()))
    settings = make_settings(tmp_path, smtp={"host": "127.0.0.1", "port": 1, "from_address": "d@example.com", "timeout": 2})
    core = CoreApp(settings, zabbix_transport=FakeZabbix().transport())
    await core.source.start(core.stop)
    from datetime import date

    result = await core.processor.run_scheduled(*core.source.current.find_subscription("a1b2"), date(2026, 10, 28))
    assert result == "failed"
    assert core.metrics.deliveries.labels(transport="email", type="digest", result="failure")._value.get() == 1
    await core.aclose()



async def test_system_mail_endpoints(tmp_path, smtp_server, caplog):
    caplog.set_level("INFO")
    controller, messages = smtp_server
    smtp = {"host": "127.0.0.1", "port": controller.port, "from_address": "digest@example.com"}
    core, client = await api_core(tmp_path, smtp=smtp)
    h = {API_KEY_HEADER: KEY}
    url = "https://digest.example.com/login/SECRET-TOKEN"
    r = await client.post("/internal/mail/login-link", headers=h,
                          json={"email": "alice@example.com", "url": url, "ttl_minutes": 15})
    assert r.status_code == 200 and r.json() == {"sent": True}
    r = await client.post("/internal/mail/unsubscribed", headers=h,
                          json={"email": "alice@example.com", "title": "#a1b2", "manage_url": "https://digest.example.com/"})
    assert r.status_code == 200
    assert [m.rcpt_tos for m in messages] == [["alice@example.com"], ["alice@example.com"]]
    assert "SECRET-TOKEN" in messages[0].content.decode()
    assert "Unsubscribed: #a1b2" in messages[1].content.decode()
    assert not any("SECRET-TOKEN" in rec.getMessage() for rec in caplog.records)
    await core.aclose()


async def test_system_mail_smtp_down(tmp_path):
    smtp = {"host": "127.0.0.1", "port": 1, "from_address": "d@example.com", "timeout": 2}
    core, client = await api_core(tmp_path, smtp=smtp)
    r = await client.post("/internal/mail/login-link", headers={API_KEY_HEADER: KEY},
                          json={"email": "alice@example.com", "url": "https://x/login/T", "ttl_minutes": 15})
    assert r.status_code == 502 and r.json()["error"] == "delivery_failed"
    assert "127.0.0.1:1" in r.json()["details"]
    await core.aclose()

async def test_identify_error_and_access_logged(tmp_path, caplog):
    caplog.set_level("INFO")
    fake = FakeZabbix()
    core, client = await api_core(tmp_path, fake)
    h = {API_KEY_HEADER: KEY}
    fake.fail_next = ["400"]
    r = await client.post("/internal/zabbix/identify", json={"token": TOKEN}, headers=h)
    assert r.status_code == 502
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(m.startswith("token identification failed: Zabbix API error: user.checkAuthentication: HTTP 400 from POST")
               for m in messages)
    assert TOKEN not in caplog.text
    await client.get("/health/live")
    await client.get("/metrics")
    access = [rec.getMessage() for rec in caplog.records if rec.name == "core.access"]
    assert len(access) == 1 and access[0].startswith("POST /internal/zabbix/identify 502 ")
    await core.aclose()
