"""The example Zabbix stub (examples/zabbix-stub) works with the real Core client."""

import importlib.util
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from common.retry import RetryConfig
from core.errors import Category, PreparationError
from core.zabbix.client import ZabbixClient
from core.zabbix.identity import validate_user
from core.zabbix.problems import fetch_problems
from core.zabbix.urls import parse_widget_url
from core.zabbix.widget import Overrides

STUB_DIR = Path(__file__).resolve().parents[3] / "examples" / "zabbix-stub"
TOKEN = "s" * 64
EMAIL = "demo@example.com"
ADMIN_TOKEN = "t" * 64
ADMIN_EMAIL = "admin@example.com"


def load_module():
    spec = importlib.util.spec_from_file_location("zabbix_stub", STUB_DIR / "stub.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stub_url():
    module = load_module()
    users = module.parse_users(f"{EMAIL}={TOKEN}, {ADMIN_EMAIL}={ADMIN_TOKEN}")
    stub = module.load_stub(str(STUB_DIR / "data.yaml"), users)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(stub))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


async def test_stub_user_and_widget(stub_url):
    client = ZabbixClient(stub_url, 5, retry=RetryConfig(attempts=1))
    try:
        identity = await validate_user(client, TOKEN, EMAIL)
        admin = await validate_user(client, ADMIN_TOKEN, ADMIN_EMAIL)
        assert admin.userid != identity.userid
        with pytest.raises(PreparationError) as info:
            await validate_user(client, ADMIN_TOKEN, EMAIL)
        assert info.value.category == Category.EMAIL_NOT_OWNED
        with pytest.raises(PreparationError) as info:
            await validate_user(client, "x" * 64, EMAIL)
        assert info.value.category == Category.ZABBIX_AUTH

        ref = parse_widget_url(f"{stub_url}/zabbix.php?action=dashboard.view&dashboardid=1")
        ps = await fetch_problems(client, TOKEN, identity.userid, ref, 100)
        assert ps.widget_name == "Linux problems" and ps.dashboard_name == "Demo"
        assert [p["eventid"] for p in ps.problems] == ["1001", "1002", "1005", "1003", "1006"]
        assert ps.total == 6 and ps.truncated
        assert ps.problems[0]["host_name"] == "db1" and ps.problems[0]["tags_display"] == ["service: db"]

        ps = await fetch_problems(client, TOKEN, identity.userid, ref, 100, overrides=Overrides(min_severity=4))
        assert [p["eventid"] for p in ps.problems] == ["1001", "1002"] and not ps.truncated

        ps = await fetch_problems(client, TOKEN, identity.userid, ref, 100,
                                  overrides=Overrides(min_severity=4, hide_acknowledged=True))
        assert [p["eventid"] for p in ps.problems] == ["1001"]
    finally:
        await client.aclose()
