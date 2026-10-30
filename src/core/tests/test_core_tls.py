"""Trusted CA certificates: ca_file, SSL_CERT_FILE/SSL_CERT_DIR, certifi. Real TLS servers."""

import json
import socket
import ssl
import threading
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import trustme
from aiosmtpd.controller import Controller

from common.retry import RetryConfig
from common.smtp import SmtpConfig, check_tls, send_email
from common.tls import ssl_context, trust_source
from core.errors import ZabbixTemporaryError
from core.zabbix.client import ZabbixClient


@pytest.fixture
def ca(tmp_path, monkeypatch):
    for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    authority = trustme.CA()
    path = tmp_path / "ca.pem"
    authority.cert_pem.write_to_path(str(path))
    return authority, str(path)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def https_url(ca):
    authority, _ = ca

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"jsonrpc": "2.0", "result": "7.0.0", "id": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert("localhost").configure_cert(ctx)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://localhost:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


async def zabbix_call(url, **kwargs):
    client = ZabbixClient(url, 5, retry=RetryConfig(attempts=1), **kwargs)
    try:
        return await client.call("apiinfo.version", {})
    finally:
        await client.aclose()


async def test_zabbix_trust_order(ca, https_url, monkeypatch, tmp_path):
    _, ca_path = ca
    # certifi does not know the test CA.
    with pytest.raises(ZabbixTemporaryError) as info:
        await zabbix_call(https_url)
    assert "CERTIFICATE_VERIFY_FAILED" in str(info.value)
    assert await zabbix_call(https_url, ca_file=ca_path) == "7.0.0"
    monkeypatch.setenv("SSL_CERT_FILE", ca_path)
    assert await zabbix_call(https_url) == "7.0.0"
    assert trust_source() == f"SSL_CERT_FILE={ca_path}"
    # ca_file wins over SSL_CERT_FILE.
    other = tmp_path / "other.pem"
    trustme.CA().cert_pem.write_to_path(str(other))
    with pytest.raises(ZabbixTemporaryError):
        await zabbix_call(https_url, ca_file=str(other))
    assert await zabbix_call(https_url, verify_tls=False) == "7.0.0"


def test_missing_ca_file_is_config_error(monkeypatch, tmp_path):
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    with pytest.raises(ValueError, match="SSL_CERT_FILE/SSL_CERT_DIR: CA file not found"):
        ssl_context()
    with pytest.raises(ValueError, match="CA file not found"):
        check_tls(SmtpConfig(security="starttls"))
    check_tls(SmtpConfig(security="none"))


async def test_smtp_uses_ssl_cert_file(ca, monkeypatch):
    authority, ca_path = ca
    received = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            received.append(envelope)
            return "250 OK"

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert("localhost").configure_cert(server_ctx)
    port = free_port()
    controller = Controller(Handler(), hostname="127.0.0.1", port=port, ssl_context=server_ctx,
                            server_hostname="localhost")
    controller.start()
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = "a@example.com", "b@example.com", "x"
        msg.set_content("x")
        config = SmtpConfig(host="localhost", port=port, security="tls", timeout=5)
        with pytest.raises(Exception, match="CERTIFICATE_VERIFY_FAILED|certificate verify failed"):
            await send_email(config, msg)
        monkeypatch.setenv("SSL_CERT_FILE", ca_path)
        await send_email(config, msg)
        assert len(received) == 1
    finally:
        controller.stop()
