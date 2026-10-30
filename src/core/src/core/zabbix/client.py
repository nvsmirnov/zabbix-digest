"""Zabbix JSON-RPC client with per-request retries and a global concurrency limit."""

import asyncio
import itertools
import logging
from typing import Any, Callable

import httpx

from common.http import describe_request_error, describe_response, proxy_options
from common.retry import RetryConfig
from common.tls import ssl_context
from ..errors import ZabbixApiError, ZabbixAuthError, ZabbixError, ZabbixTemporaryError

log = logging.getLogger(__name__)

# Substrings of Zabbix error "data" that mean the credentials are not accepted.
_AUTH_MARKERS = (
    "not authorized",
    "not authorised",
    "session terminated",
    "re-login",
    "api token expired",
    "incorrect user name or password",
    "account is blocked",
)


def classify_rpc_error(error: dict) -> ZabbixError:
    code = error.get("code")
    message = str(error.get("message", ""))[:200]
    data = str(error.get("data", ""))[:500]
    text = f"{message} {data}".lower()
    summary = f"Zabbix API error {code}: {message} {data}".strip()
    if any(marker in text for marker in _AUTH_MARKERS):
        return ZabbixAuthError(summary)
    if code == -32603 or "database error" in text:
        return ZabbixTemporaryError(summary)
    return ZabbixApiError(summary)


class ZabbixClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        verify_tls: bool = True,
        ca_file: str | None = None,
        max_concurrent: int = 10,
        retry: RetryConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        on_error: Callable[[str], None] | None = None,
        on_retry: Callable[[], None] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        use_proxy: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api_jsonrpc.php"
        self._timeout = timeout
        self.retry = retry or RetryConfig()
        self._sem = asyncio.Semaphore(max_concurrent)
        self._ids = itertools.count(1)
        self._on_error = on_error or (lambda kind: None)
        self._on_retry = on_retry or (lambda: None)
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            timeout=timeout, verify=ssl_context(verify_tls, ca_file), transport=transport, **proxy_options(use_proxy)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call_once(self, method: str, params: Any, token: str | None) -> Any:
        headers = {"Content-Type": "application/json-rpc"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": next(self._ids)}
        async with self._sem:
            try:
                response = await self._http.post(self.api_url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise ZabbixTemporaryError(
                    f"{method}: request timed out after {self._timeout:g}s: {describe_request_error(exc)}"
                ) from None
            except httpx.TransportError as exc:
                raise ZabbixTemporaryError(f"{method}: request failed: {describe_request_error(exc)}") from None
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise ZabbixTemporaryError(f"{method}: {describe_response(response)}")
        if response.status_code in (401, 403):
            raise ZabbixAuthError(f"{method}: {describe_response(response)}")
        if response.status_code >= 400:
            raise ZabbixApiError(f"{method}: {describe_response(response)}")
        try:
            body = response.json()
        except ValueError:
            raise ZabbixTemporaryError(f"{method}: response is not JSON: {describe_response(response)}") from None
        if not isinstance(body, dict):
            raise ZabbixTemporaryError(f"{method}: unexpected response: {describe_response(response)}")
        if "error" in body:
            raise classify_rpc_error(body["error"] or {})
        if "result" not in body:
            raise ZabbixTemporaryError(f"{method}: response has no result")
        return body["result"]

    async def call(self, method: str, params: Any, token: str | None = None) -> Any:
        attempt = 1
        while True:
            try:
                return await self._call_once(method, params, token)
            except ZabbixTemporaryError as exc:
                self._on_error("temporary")
                extra = {"operation": "zabbix_api", "error_category": "zabbix_unavailable"}
                if attempt >= self.retry.attempts:
                    log.warning("zabbix request failed (attempt %d/%d): %s", attempt, self.retry.attempts, exc,
                                extra=extra)
                    log.error("zabbix request %s failed after %d attempts, giving up", method, attempt, extra=extra)
                    raise
                delay = self.retry.delay(attempt)
                log.warning(
                    "zabbix request failed, retrying in %.1fs (attempt %d/%d): %s",
                    delay, attempt, self.retry.attempts, exc, extra=extra,
                )
                self._on_retry()
                await self._sleep(delay)
                attempt += 1
            except ZabbixAuthError:
                self._on_error("auth")
                raise
            except ZabbixApiError:
                self._on_error("api")
                raise
