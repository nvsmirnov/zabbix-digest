"""Web backend tests against a real in-process Core (fake Zabbix)."""

import asyncio
import logging
import re

import httpx
import pytest

from common.yamlio import dumps_yaml, load_yaml
from core.api import create_app as create_core_app
from core.app import CoreApp
from core.config import CoreSettings
from web.app import create_app
from web.config import WebSettings

from fake_zabbix import OTHER_TOKEN, TOKEN, WIDGET_URL, FakeZabbix

KEY = "k" * 32
H = {"X-Requested-With": "zabbix-digest"}


class Env:
    def __init__(self, tmp_path, users: dict | None = None, **web_overrides) -> None:
        self.tmp = tmp_path
        self.users_file = tmp_path / "users.yaml"
        if users is not None:
            self.users_file.write_text(dumps_yaml(users))
        self.zabbix = FakeZabbix()
        self.core = CoreApp(
            CoreSettings(
                zabbix={"url": "https://zabbix.example.com", "retry": {"attempts": 1}},
                users={"source": "http", "cache_file": str(tmp_path / "cache.yaml"), "startup_retry": {"attempts": 1}},
                frontend={"url": "http://web", "public_url": "https://digest.example.com"},
                internal_api_key=KEY,
                state_dir=str(tmp_path / "state"),
                scheduler={"timezone": "Europe/Berlin"},
                transports={"email": {"type": "email", "public": True}, "hidden": {"type": "email", "public": False}},
                http={"enabled": False},
            ),
            zabbix_transport=self.zabbix.transport(),
        )
        self.sent = []

        class Rec:
            supports_preview = True

            def __init__(inner, real):
                inner.real = real

            async def send(inner, message):
                self.sent.append(message)

            def render_preview(inner, message):
                return inner.real.render_preview(message)

        for t in self.core.transports.transports.values():
            t.adapter = Rec(t.adapter)
        self.core_down = False
        core_asgi = httpx.ASGITransport(app=create_core_app(self.core))

        env = self

        class CoreTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if env.core_down:
                    raise httpx.ConnectError("down")
                return await core_asgi.handle_async_request(request)

        settings = WebSettings(
            internal_api_key=KEY,
            session_secret="s" * 40,
            users_file=str(self.users_file),
            state_dir=str(tmp_path / "web-state"),
            core_url="http://core",
            public_url="https://digest.example.com",
            admin_emails="admin@example.com",
            cookie_secure=False,
            static_dir=None,
            **web_overrides,
        )
        self.app = create_app(settings, core_transport=CoreTransport())
        self.web = self.app.state.web
        # System emails are sent by Core (through its internal API).
        self.mails = []
        self.mail_error: Exception | None = None

        async def login_link(to, url, ttl):
            if self.mail_error:
                raise self.mail_error
            self.mails.append(("login", to, url))

        async def unsubscribed(to, title, url):
            if self.mail_error:
                raise self.mail_error
            self.mails.append(("unsubscribed", to, title))

        self.core.mailer.send_login_link = login_link
        self.core.mailer.send_unsubscribed = unsubscribed
        # Core fetches its configuration from this web app.
        web_asgi = httpx.ASGITransport(app=self.app)
        self.core.source._http = httpx.AsyncClient(
            transport=web_asgi, base_url="http://web", headers={"X-Internal-Api-Key": KEY}
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test", headers=H)

    def data(self) -> dict:
        return load_yaml(self.users_file)

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        if self.web.store._notify_tasks:
            await asyncio.gather(*self.web.store._notify_tasks)
        if self.web._background:
            await asyncio.gather(*self.web._background)


async def register(env: Env, client: httpx.AsyncClient, token=TOKEN, email="alice@example.com"):
    r = await client.post("/api/auth/token", json={"token": token})
    assert r.status_code == 200, r.text
    assert r.json()["step"] == "choose_email"
    r = await client.post("/api/auth/token", json={"token": token, "email": email})
    assert r.status_code == 200, r.text
    return r


async def test_registration_and_subscription_lifecycle(tmp_path):
    env = Env(tmp_path)
    async with env.client() as c:
        assert (await c.get("/api/me")).status_code == 401
        await register(env, c)
        me = (await c.get("/api/me")).json()
        assert me["email"] == "alice@example.com" and me["timezone"] == "Europe/Berlin"
        assert [t["name"] for t in me["transports"]] == ["email"]
        assert "zabbix_token" not in str(me) and TOKEN not in str(me)

        r = await c.post("/api/subscriptions", json={"name": "DB", "widget_url": WIDGET_URL, "time": "08:00"})
        assert r.status_code == 200, r.text
        sub = r.json()
        assert re.fullmatch(r"[0-9a-f]{4}", sub["id"]) and sub["title"] == f"DB (#{sub['id']})"
        stored = env.data()
        assert stored["revision"] == 2
        assert stored["users"][0]["subscriptions"][0]["unsubscribe_token"]
        await env.settle()
        assert env.core.source.current.revision == 2

        # Core validation rejects bad input and nothing is saved.
        r = await c.post("/api/subscriptions", json={"widget_url": "https://x/zabbix.php?action=dashboard.view", "time": "08:00"})
        assert r.status_code == 422 and env.data()["revision"] == 2

        # Non-public transport cannot be selected.
        r = await c.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "08:00", "transport": "hidden"})
        assert r.status_code == 400

        r = await c.put(f"/api/subscriptions/{sub['id']}", json={"widget_url": WIDGET_URL, "time": "09:15", "enabled": False})
        assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["title"] == f"#{sub['id']}"

        # Minimal severity: stored only when set, validated by Core.
        r = await c.put(f"/api/subscriptions/{sub['id']}", json={"widget_url": WIDGET_URL, "time": "09:15", "min_severity": 4})
        assert r.status_code == 200 and r.json()["min_severity"] == 4
        assert env.data()["users"][0]["subscriptions"][0]["min_severity"] == 4
        r = await c.put(f"/api/subscriptions/{sub['id']}", json={"widget_url": WIDGET_URL, "time": "09:15", "min_severity": 9})
        assert r.status_code == 422 and env.data()["users"][0]["subscriptions"][0]["min_severity"] == 4
        r = await c.put(f"/api/subscriptions/{sub['id']}", json={"widget_url": WIDGET_URL, "time": "09:15", "enabled": False})
        assert r.json()["min_severity"] is None and "min_severity" not in env.data()["users"][0]["subscriptions"][0]
        r = await c.put(f"/api/subscriptions/{sub['id']}",
                        json={"widget_url": WIDGET_URL, "time": "09:15", "enabled": False, "hide_acknowledged": True})
        stored = env.data()["users"][0]["subscriptions"][0]
        assert r.json()["hide_acknowledged"] is True and r.json()["hide_suppressed"] is False
        assert stored["hide_acknowledged"] is True and "hide_suppressed" not in stored

        r = await c.post(f"/api/subscriptions/{sub['id']}/preview")
        assert r.status_code == 200 and "Memory low" in r.json()["body"]
        r = await c.post(f"/api/subscriptions/{sub['id']}/test-send")
        assert r.status_code == 200 and env.sent[-1]["type"] == "test"
        # hide_acknowledged is set above: "CPU high" is excluded.
        r = await c.post(f"/api/subscriptions/{sub['id']}/send-now")
        assert r.status_code == 200 and r.json() == {"result": "delivered", "shown": 2, "total": 2, "total_exact": True}
        assert env.sent[-1]["type"] == "digest"
        env.zabbix.down = True
        r = await c.post(f"/api/subscriptions/{sub['id']}/send-now")
        assert r.status_code == 503 and r.json()["error"] == "zabbix_unavailable"
        env.zabbix.down = False

        r = await c.delete(f"/api/subscriptions/{sub['id']}")
        assert r.status_code == 200 and env.data()["users"][0]["subscriptions"] == []


async def test_csrf_header_required(tmp_path):
    env = Env(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://test") as c:
        r = await c.post("/api/auth/token", json={"token": TOKEN})
        assert r.status_code == 403


async def test_core_unavailable_does_not_save(tmp_path):
    env = Env(tmp_path)
    async with env.client() as c:
        await register(env, c)
        env.core_down = True
        r = await c.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "08:00"})
        assert r.status_code == 503 and "administrator" in r.json()["message"]
        assert env.data()["users"][0]["subscriptions"] == []


async def test_revision_conflict(tmp_path):
    env = Env(tmp_path)
    async with env.client() as c:
        await register(env, c)
        original = env.web.core.validate

        async def validate_and_race(config):
            await original(config)
            data = env.data()
            data["revision"] += 1
            env.users_file.write_text(dumps_yaml(data))

        env.web.core.validate = validate_and_race
        r = await c.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "08:00"})
        assert r.status_code == 409
        env.web.core.validate = original
        r = await c.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "08:00"})
        assert r.status_code == 200


async def test_existing_email_and_foreign_token(tmp_path):
    env = Env(tmp_path)
    env.zabbix.users[OTHER_TOKEN]["medias"][0]["sendto"].append("alice@example.com")
    async with env.client() as c:
        await register(env, c)
        assert len(env.data()["users"]) == 1
    async with env.client() as c2:
        r = await c2.post("/api/auth/token", json={"token": OTHER_TOKEN, "email": "alice@example.com"})
        assert r.status_code == 403 and r.json()["error"] == "other_user"
        assert len(env.data()["users"]) == 1


async def test_email_login_link_flow(tmp_path):
    env = Env(tmp_path)
    async with env.client() as c:
        await register(env, c)
    async with env.client() as c:
        r1 = await c.post("/api/auth/email", json={"email": "alice@example.com"})
        r2 = await c.post("/api/auth/email", json={"email": "nobody@example.com"})
        assert r1.json() == r2.json()
        await env.settle()
        assert [m[1] for m in env.mails] == ["alice@example.com"]
        token = env.mails[0][2].rsplit("/", 1)[1]
        r = await c.post("/api/auth/link", json={"token": token})
        assert r.status_code == 200
        assert (await c.get("/api/me")).status_code == 200
        # Replay is rejected.
        r = await c.post("/api/auth/link", json={"token": token})
        assert r.status_code == 400


async def test_login_link_with_revoked_token(tmp_path):
    env = Env(tmp_path)
    async with env.client() as c:
        await register(env, c)
    gen = env.data()["users"][0]["generation"]
    token = env.web.links.create(gen)
    del env.zabbix.users[TOKEN]
    async with env.client() as c:
        r = await c.post("/api/auth/link", json={"token": token})
        assert r.status_code == 409 and r.json()["error"] == "token_required"
    # Zabbix outage fails only the current attempt; the link stays usable.
    env.zabbix.users[TOKEN] = FakeZabbix().users[TOKEN]
    env.zabbix.down = True
    async with env.client() as c:
        assert (await c.post("/api/auth/link", json={"token": token})).status_code == 503
    env.zabbix.down = False
    async with env.client() as c:
        assert (await c.post("/api/auth/link", json={"token": token})).status_code == 200


async def test_rate_limit(tmp_path):
    env = Env(tmp_path, rate_limits={"login_link_per_email": {"limit": 2, "period": 3600}})
    async with env.client() as c:
        for _ in range(2):
            assert (await c.post("/api/auth/email", json={"email": "x@example.com"})).status_code == 200
        assert (await c.post("/api/auth/email", json={"email": "x@example.com"})).status_code == 429


async def test_logout_all_and_sessions_survive_zabbix_outage(tmp_path):
    env = Env(tmp_path)
    async with env.client() as a, env.client() as b:
        await register(env, a)
        b.cookies = a.cookies
        env.zabbix.down = True
        assert (await b.get("/api/me")).status_code == 200
        r = await b.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "07:00"})
        assert r.status_code == 200
        assert (await b.post("/api/profile/token", json={"token": OTHER_TOKEN})).status_code == 503
        env.zabbix.down = False
        assert (await a.post("/api/auth/logout-all")).status_code == 200
        assert (await b.get("/api/me")).status_code == 401
        assert env.data()["users"][0]["session_version"] == 2


async def test_token_replacement(tmp_path):
    env = Env(tmp_path)
    async with env.client() as c:
        await register(env, c)
        new_token = "n" * 64
        env.zabbix.users[new_token] = dict(env.zabbix.users[TOKEN])
        assert (await c.post("/api/profile/token", json={"token": new_token})).status_code == 200
        assert env.data()["users"][0]["zabbix_token"] == new_token
        # Token of another Zabbix user is rejected, existing token kept.
        r = await c.post("/api/profile/token", json={"token": OTHER_TOKEN})
        assert r.status_code == 403 and env.data()["users"][0]["zabbix_token"] == new_token


async def test_email_change_keeps_subscriptions(tmp_path):
    env = Env(tmp_path)
    env.zabbix.users[TOKEN]["medias"][0]["sendto"].append("alice2@example.com")
    async with env.client() as c:
        await register(env, c)
        sub = (await c.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "08:00"})).json()
        before = env.data()["users"][0]["subscriptions"][0]
        emails = (await c.get("/api/profile/emails")).json()["emails"]
        assert {e["email"] for e in emails} == {"alice@example.com", "alice2@example.com"}
        r = await c.post("/api/profile/email", json={"email": "alice2@example.com"})
        assert r.status_code == 200
        after = env.data()["users"][0]
        assert after["email"] == "alice2@example.com" and after["subscriptions"][0] == before
        assert (await c.get("/api/me")).json()["email"] == "alice2@example.com"
        assert (await c.post("/api/profile/email", json={"email": "evil@example.com"})).status_code == 400
        assert sub["id"] == after["subscriptions"][0]["id"]


async def test_profile_deletion(tmp_path):
    env = Env(tmp_path)
    async with env.client() as c:
        await register(env, c)
        await c.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "08:00"})
        token = env.data()["users"][0]["subscriptions"][0]["unsubscribe_token"]
        old_cookie = c.cookies.get("zd_session")
        assert (await c.post("/api/profile/delete", json={"confirm": "delete"})).status_code == 400
        assert (await c.post("/api/profile/delete", json={"confirm": "DELETE"})).status_code == 200
        assert env.data()["users"] == []
        assert (await c.get(f"/api/unsubscribe/{token}")).status_code == 404

    async def me_with(cookie: str) -> int:
        async with env.client() as other:
            other.cookies.set("zd_session", cookie)
            return (await other.get("/api/me")).status_code

    assert await me_with(old_cookie) == 401
    # Registering again creates a new generation; the old cookie stays invalid.
    async with env.client() as c:
        await register(env, c)
        assert (await c.get("/api/me")).status_code == 200
    assert await me_with(old_cookie) == 401


async def test_unsubscribe_flow(tmp_path):
    env = Env(tmp_path)
    async with env.client() as c:
        await register(env, c)
        await c.post("/api/subscriptions", json={"name": "DB", "widget_url": WIDGET_URL, "time": "08:00"})
    token = env.data()["users"][0]["subscriptions"][0]["unsubscribe_token"]
    async with env.client() as anon:
        info = (await anon.get(f"/api/unsubscribe/{token}")).json()
        assert info["enabled"] is True
        # Opening the link does not change anything.
        assert env.data()["users"][0]["subscriptions"][0]["enabled"] is True
        assert (await anon.post(f"/api/unsubscribe/{token}")).json() == {"status": "disabled"}
        rev = env.data()["revision"]
        assert (await anon.post(f"/api/unsubscribe/{token}")).json() == {"status": "already_disabled"}
        assert env.data()["revision"] == rev
        await env.settle()
        assert [m[0] for m in env.mails] == ["unsubscribed"]
        assert env.data()["users"][0]["subscriptions"][0]["enabled"] is False
        assert (await anon.get("/api/unsubscribe/" + "x" * 32)).status_code == 404


async def test_unsubscribe_link_in_digest(tmp_path):
    env = Env(tmp_path)
    async with env.client() as c:
        await register(env, c)
        sub = (await c.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "08:00"})).json()
    await env.settle()
    token = env.data()["users"][0]["subscriptions"][0]["unsubscribe_token"]
    user, s = env.core.source.current.find_subscription(sub["id"])
    msg = await env.core.processor.prepare_digest(user, s)
    assert msg["unsubscribe"]["url"] == f"https://digest.example.com/unsubscribe/{token}"


async def test_impersonation(tmp_path):
    admin_token = "d" * 64
    env = Env(tmp_path)
    env.zabbix.users[admin_token] = {
        "userid": "1", "username": "admin",
        "medias": [{"sendto": ["admin@example.com"], "active": "0"}], "usrgrps": [],
    }
    async with env.client() as alice:
        await register(env, alice)
        assert (await alice.post("/api/admin/impersonate", json={"email": "admin@example.com"})).status_code == 403
    async with env.client() as admin:
        await register(env, admin, token=admin_token, email="admin@example.com")
        assert (await admin.get("/api/me")).json()["is_admin"] is True
        assert (await admin.post("/api/admin/impersonate", json={"email": "alice@example.com"})).status_code == 200
        me = (await admin.get("/api/me")).json()
        assert me["email"] == "alice@example.com" and me["impersonating"] and me["real_email"] == "admin@example.com"
        r = await admin.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "08:00"})
        assert r.status_code == 200
        alice_subs = next(u for u in env.data()["users"] if u["email"] == "alice@example.com")["subscriptions"]
        assert len(alice_subs) == 1
        # Target invalidates all sessions: impersonation ends, admin session stays.
        gen = next(u for u in env.data()["users"] if u["email"] == "alice@example.com")["generation"]

        def bump(cfg):
            for u in cfg["users"]:
                if u["generation"] == gen:
                    u["session_version"] += 1
            return True

        await env.web.store.mutate(bump)
        me = (await admin.get("/api/me")).json()
        assert me["email"] == "admin@example.com" and not me["impersonating"]
        assert (await admin.post("/api/admin/impersonate", json={"email": "alice@example.com"})).status_code == 200
        assert (await admin.post("/api/admin/stop-impersonation")).status_code == 200
        assert (await admin.get("/api/me")).json()["email"] == "admin@example.com"


async def test_impersonation_cookie_expires(tmp_path, monkeypatch):
    admin_token = "d" * 64
    env = Env(tmp_path, impersonation_ttl=60)
    env.zabbix.users[admin_token] = {"userid": "1", "username": "admin",
                                     "medias": [{"sendto": ["admin@example.com"], "active": "0"}], "usrgrps": []}
    async with env.client() as alice:
        await register(env, alice)
    async with env.client() as admin:
        await register(env, admin, token=admin_token, email="admin@example.com")
        await admin.post("/api/admin/impersonate", json={"email": "alice@example.com"})
        import time as time_module

        real = time_module.time
        monkeypatch.setattr("web.auth.time.time", lambda: real() + 120)
        assert (await admin.get("/api/me")).json()["email"] == "admin@example.com"


async def test_internal_config_and_health(tmp_path):
    env = Env(tmp_path)
    async with env.client() as c:
        await register(env, c)
        assert (await c.get("/internal/config")).status_code == 401
        r = await c.get("/internal/config", headers={"X-Internal-Api-Key": KEY})
        assert r.status_code == 200 and r.json()["users"][0]["email"] == "alice@example.com"
        assert r.headers["cache-control"] == "no-store"
        assert (await c.get("/health/live")).status_code == 200
        assert (await c.get("/health/ready")).status_code == 200
        metrics = (await c.get("/metrics")).text
        assert "zabbix_digest_web_http_requests_total" in metrics and "alice" not in metrics
        pub = (await c.get("/api/public-config")).json()
        assert set(pub) == {"version", "sentry_dsn", "sentry_environment", "help_url"} and pub["help_url"] is None


@pytest.mark.parametrize("value, expected", [("", None), ("  ", None), ("https://wiki.example.com/digest", "https://wiki.example.com/digest")])
def test_help_url(monkeypatch, value, expected):
    monkeypatch.setenv("HELP_URL", value)
    assert WebSettings(internal_api_key=KEY, session_secret="s" * 40).help_url == expected


def test_help_url_rejects_other_schemes(monkeypatch):
    monkeypatch.setenv("HELP_URL", "javascript:alert(1)")
    with pytest.raises(ValueError, match="help_url"):
        WebSettings(internal_api_key=KEY, session_secret="s" * 40)


async def test_subscription_limit(tmp_path):
    env = Env(tmp_path, max_subscriptions_per_user=1)
    async with env.client() as c:
        await register(env, c)
        assert (await c.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "08:00"})).status_code == 200
        r = await c.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "09:00"})
        assert r.status_code == 400 and r.json()["error"] == "limit"


@pytest.mark.parametrize("value, expected", [("a@x.com, B@x.com", ["a@x.com", "b@x.com"]), ('["c@x.com"]', ["c@x.com"])])
def test_admin_emails_env(monkeypatch, value, expected):
    monkeypatch.setenv("ADMIN_EMAILS", value)
    settings = WebSettings(internal_api_key=KEY, session_secret="s" * 40)
    assert settings.admin_emails == expected



async def test_login_link_failure_logged(tmp_path, caplog):
    caplog.set_level("INFO")
    env = Env(tmp_path)
    async with env.client() as c:
        await register(env, c)
    env.mail_error = ConnectionRefusedError(111, "Connection refused")
    async with env.client() as c:
        r = await c.post("/api/auth/email", json={"email": "alice@example.com"})
        assert r.status_code == 200
        await env.settle()
    messages = [rec.getMessage() for rec in caplog.records if not rec.name.startswith("httpx")]
    core_line = next(m for m in messages if m.startswith("login_link not sent"))
    assert "SMTP " in core_line and "Connection refused" in core_line
    web_line = next(m for m in messages if m.startswith("login link not sent"))
    assert "HTTP 502 delivery_failed" in web_line and "Connection refused" in web_line
    assert not any("/login/" in m for m in messages)


async def test_logs_name_user_and_subscription(tmp_path):
    from common.logs import _ContextDefaults

    records = []

    class Handler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Handler(level=logging.INFO)
    handler.addFilter(_ContextDefaults("test"))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        env = Env(tmp_path)
        async with env.client() as c:
            await register(env, c)
            sub = (await c.post("/api/subscriptions", json={"widget_url": WIDGET_URL, "time": "08:00"})).json()
            assert (await c.post(f"/api/subscriptions/{sub['id']}/test-send")).status_code == 200
            env.zabbix.down = True
            assert (await c.post(f"/api/subscriptions/{sub['id']}/preview")).status_code == 503
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    sent = next(r for r in records if r.getMessage() == "test message sent")
    failed = next(r for r in records if r.name == "web.app" and "preview failed" in r.getMessage())
    for record in (sent, failed):
        assert record.user == "alice@example.com" and record.subscription_id == sub["id"]

async def test_core_errors_and_access_logged(tmp_path, caplog):
    caplog.set_level("INFO")
    env = Env(tmp_path)
    async with env.client() as c:
        env.zabbix.fail_next = ["400"]
        r = await c.post("/api/auth/token", json={"token": TOKEN})
        assert r.status_code == 502
        messages = [rec.getMessage() for rec in caplog.records]
        assert any(m.startswith("token identification failed: core answered HTTP 502 zabbix_error: "
                                "user.checkAuthentication: HTTP 400") for m in messages)
        await c.get("/unsubscribe/secret-token-value-123")
        await c.get("/health/live")
    access = [rec.getMessage() for rec in caplog.records if rec.name == "web.access"]
    assert access[0].startswith("POST /api/auth/token 502 ")
    assert any(m.startswith("GET /unsubscribe/{token} ") for m in access)
    # The httpx logger of the test client is muted in production (common.logs).
    ours = "\n".join(rec.getMessage() for rec in caplog.records if rec.name != "httpx")
    assert not any("/health/" in m for m in access) and "secret-token-value-123" not in ours
    assert TOKEN not in caplog.text
