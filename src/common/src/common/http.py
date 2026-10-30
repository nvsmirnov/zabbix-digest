"""Descriptions of HTTP requests, responses and proxies for logs and error details.

URLs are shown without query string and credentials.
"""

import urllib.request
from urllib.parse import urlsplit, urlunsplit

import httpx

from .errors import describe_error

BODY_EXCERPT = 200
# Request extension set on requests of clients with proxies disabled (see proxy_options).
_NO_PROXY_MARK = "zabbix_digest_no_proxy"


def safe_url(url: str | httpx.URL) -> str:
    """URL without credentials, query and fragment."""
    parts = urlsplit(str(url))
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def proxy_for(url: str | httpx.URL) -> str | None:
    """Proxy that httpx takes from the environment (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY)."""
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    proxies = urllib.request.getproxies_environment()
    if not proxies or urllib.request.proxy_bypass_environment(host, proxies):
        return None
    proxy = proxies.get(parts.scheme) or proxies.get("all")
    return safe_url(proxy) if proxy else None


async def _mark_no_proxy(request: httpx.Request) -> None:
    request.extensions[_NO_PROXY_MARK] = True


def proxy_options(use_proxy: bool) -> dict:
    """httpx client options. use_proxy=False ignores proxies from the environment.

    trust_env stays on, so SSL_CERT_FILE and SSL_CERT_DIR still apply. The http:// and
    https:// patterns are needed: proxies from HTTP_PROXY and HTTPS_PROXY are more
    specific than all:// and would win over it.
    """
    if use_proxy:
        return {}
    return {
        "mounts": {"http://": None, "https://": None, "all://": None},
        "event_hooks": {"request": [_mark_no_proxy]},
    }


def _target(request: httpx.Request) -> str:
    text = f"{request.method} {safe_url(request.url)}"
    proxy = None if request.extensions.get(_NO_PROXY_MARK) else proxy_for(request.url)
    return text + (f" via proxy {proxy}" if proxy else "")


def describe_request_error(exc: httpx.HTTPError) -> str:
    """E.g. "GET http://web:8080/internal/config: ConnectError: [Errno 111] Connection refused"."""
    try:
        return f"{_target(exc.request)}: {describe_error(exc)}"
    except RuntimeError:  # the exception has no request
        return describe_error(exc)


def describe_response(response: httpx.Response) -> str:
    """E.g. "HTTP 503 from GET http://web:8080/internal/config via proxy http://proxy:3128: <body excerpt>"."""
    text = f"HTTP {response.status_code} from {_target(response.request)}"
    server = response.headers.get("server")
    if server:
        text += f" (server: {server[:60]})"
    try:
        body = " ".join(response.text.split())
    except Exception:  # noqa: BLE001
        body = ""
    if body:
        text += ": " + (body if len(body) <= BODY_EXCERPT else body[: BODY_EXCERPT - 3] + "...")
    return text


def proxy_summary() -> str:
    """Proxy settings from the environment, for the startup log."""
    proxies = urllib.request.getproxies_environment()
    if not proxies:
        return "none"
    items = []
    for key in sorted(proxies):
        value = proxies[key] if key == "no" else safe_url(proxies[key])
        items.append(f"{key}={value}")
    return ", ".join(items)
