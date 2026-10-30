import asyncio

import pytest
import httpx
import yaml

from common.errors import describe_error
from common.http import describe_request_error, describe_response, proxy_summary, safe_url
from core.errors import ZabbixTemporaryError

from fake_zabbix import TOKEN, FakeZabbix
from test_core_widget import client


def test_describe_error_message_and_root_cause():
    assert describe_error(ValueError("bad value")) == "ValueError: bad value"
    assert describe_error(RuntimeError()) == "RuntimeError"
    try:
        try:
            raise ConnectionRefusedError(111, "Connection refused")
        except OSError as exc:
            raise OSError("All connection attempts failed") from exc
    except OSError as exc:
        text = describe_error(exc)
    assert text == "OSError: All connection attempts failed (ConnectionRefusedError: [Errno 111] Connection refused)"


def test_describe_error_skips_cancellation():
    try:
        try:
            raise asyncio.CancelledError("Cancelled via cancel scope")
        except asyncio.CancelledError as exc:
            raise TimeoutError() from exc
    except TimeoutError as exc:
        assert describe_error(exc) == "TimeoutError"


def test_describe_error_yaml_hides_source_line():
    with pytest.raises(yaml.YAMLError) as info:
        yaml.safe_load(f"a: 1\n  token: {TOKEN}: x\n")
    text = describe_error(info.value)
    assert TOKEN not in text and "line 2" in text


async def test_zabbix_connection_error_is_described():
    fake = FakeZabbix()
    fake.down = True
    with pytest.raises(ZabbixTemporaryError) as info:
        await client(fake, attempts=1).call("user.checkAuthentication", {"token": TOKEN})
    assert str(info.value) == (
        "user.checkAuthentication: request failed: POST https://zabbix.example.com/api_jsonrpc.php: ConnectError: down"
    )


async def test_zabbix_every_attempt_and_final_failure_logged(caplog):
    fake = FakeZabbix()
    fake.down = True
    with pytest.raises(ZabbixTemporaryError):
        await client(fake, attempts=2).call("user.get", {}, token=TOKEN)
    records = [r for r in caplog.records if r.name == "core.zabbix.client"]
    assert [r.levelname for r in records] == ["WARNING", "WARNING", "ERROR"]
    assert "(attempt 1/2)" in records[0].getMessage() and "(attempt 2/2)" in records[1].getMessage()
    assert "ConnectError: down" in records[1].getMessage()
    assert records[2].getMessage() == "zabbix request user.get failed after 2 attempts, giving up"


def test_safe_url_hides_credentials_and_query():
    assert safe_url("http://user:pw@proxy:3128/a?token=x#f") == "http://proxy:3128/a"


def test_http_descriptions_show_url_proxy_and_body(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@proxy.local:3128")
    monkeypatch.setenv("NO_PROXY", "localhost")
    request = httpx.Request("GET", "http://web:8080/internal/config?x=1")
    response = httpx.Response(503, text="<html><body>Service   Unavailable</body></html>",
                              headers={"Server": "squid"}, request=request)
    assert describe_response(response) == (
        "HTTP 503 from GET http://web:8080/internal/config via proxy http://proxy.local:3128 (server: squid): "
        "<html><body>Service Unavailable</body></html>"
    )
    local = httpx.ConnectError("refused", request=httpx.Request("GET", "http://localhost:8081/x"))
    assert describe_request_error(local) == "GET http://localhost:8081/x: ConnectError: refused"
    assert "secret" not in proxy_summary() and "no=localhost" in proxy_summary()


async def test_frontend_error_names_url(tmp_path):
    from core.config import CoreSettings
    from core.metrics import CoreMetrics
    from core.source import HttpSource, SourceError
    from core.transports.base import build_registry

    settings = CoreSettings(
        users={"source": "http", "cache_file": str(tmp_path / "c.yaml")},
        frontend={"url": "http://web:8080"}, internal_api_key="k" * 32, state_dir=str(tmp_path),
    )
    metrics = CoreMetrics()
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="busy"))
    source = HttpSource(settings, build_registry(settings, metrics), metrics, transport=transport)
    with pytest.raises(SourceError) as info:
        await source.fetch()
    assert str(info.value).startswith("frontend request failed: HTTP 503 from GET http://web:8080/internal/config")
    await source.aclose()


def test_settings_summaries_have_no_secrets(tmp_path):
    from core.config import CoreSettings, resolve_timezone, settings_summary
    from web.config import WebSettings, settings_summary as web_summary

    core = CoreSettings(internal_api_key="K" * 32, smtp={"password": "P" * 20, "username": "u"}, sentry_dsn="https://s@x/1")
    text = "\n".join(settings_summary(core, resolve_timezone("UTC")))
    assert "http://localhost" in text and "K" * 32 not in text and "P" * 20 not in text and "s@x" not in text
    web = WebSettings(internal_api_key="K" * 32, session_secret="S" * 40)
    text = "\n".join(web_summary(web))
    assert "http://localhost:8081" in text and "K" * 32 not in text and "S" * 40 not in text


@pytest.mark.parametrize("use_proxy", [False, True])
async def test_internal_requests_proxy_setting(monkeypatch, use_proxy):
    from common.http import proxy_options

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    async with httpx.AsyncClient(**proxy_options(use_proxy)) as client:
        transport = client._transport_for_url(httpx.URL("http://web:8080/internal/config"))
        assert (transport is not client._transport) is use_proxy
        request = httpx.Request("GET", "http://web:8080/internal/config")
        for hook in client.event_hooks["request"]:
            await hook(request)
        text = describe_response(httpx.Response(503, request=request))
        assert ("via proxy" in text) is use_proxy


@pytest.mark.parametrize("use_proxy", [False, True])
async def test_zabbix_client_proxy_setting(monkeypatch, use_proxy):
    from core.zabbix.client import ZabbixClient

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    zabbix = ZabbixClient("https://zabbix.example.com", use_proxy=use_proxy)
    http = zabbix._http
    assert (http._transport_for_url(httpx.URL(zabbix.api_url)) is not http._transport) is use_proxy
    await zabbix.aclose()
