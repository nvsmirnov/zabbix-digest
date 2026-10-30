"""Client for the Core internal API."""

import logging
import time
from typing import Any

import httpx

from common.http import describe_request_error, describe_response, proxy_options
from common.tls import ssl_context

from .metrics import WebMetrics

log = logging.getLogger(__name__)

API_KEY_HEADER = "X-Internal-Api-Key"


class CoreUnavailable(Exception):
    """Core could not be reached or failed unexpectedly."""


class ConfigRejected(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class CoreOperationError(Exception):
    """Core answered with an expected error (identify, preview, sending)."""

    def __init__(self, status: int, code: str, message: str, details: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def describe(self) -> str:
        """For logs, e.g. "HTTP 502 zabbix_error: user.checkAuthentication: ..."."""
        text = f"HTTP {self.status} {self.code}: {self.message}"
        return text + (f" ({self.details})" if self.details else "")


class CoreClient:
    INFO_TTL = 60.0

    def __init__(self, base_url: str, api_key: str, timeout: float, metrics: WebMetrics,
                 transport: httpx.AsyncBaseTransport | None = None, use_proxy: bool = False) -> None:
        self.metrics = metrics
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, headers={API_KEY_HEADER: api_key}, transport=transport,
            verify=ssl_context(), **proxy_options(use_proxy),
        )
        self._info: dict | None = None
        self._info_at = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, operation: str, path: str, body: Any) -> httpx.Response:
        try:
            response = await self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            self.metrics.core_failures.labels(operation=operation).inc()
            log.warning("core request failed: %s: %s", operation, describe_request_error(exc))
            raise CoreUnavailable() from None
        # 502/503 carry expected Zabbix or delivery errors; other 5xx and auth errors mean Core is not usable.
        status = response.status_code
        if status in (401, 403, 404) or (status >= 500 and status not in (502, 503)):
            self.metrics.core_failures.labels(operation=operation).inc()
            log.warning("core request failed: %s: %s", operation, describe_response(response))
            raise CoreUnavailable()
        return response

    def _error(self, response: httpx.Response) -> CoreOperationError:
        try:
            data = response.json()
        except ValueError:
            # Not an answer of Core (e.g. a proxy error page).
            log.warning("unexpected response from core: %s", describe_response(response))
            data = {}
        return CoreOperationError(
            response.status_code, str(data.get("error", "error")), str(data.get("message", "Operation failed.")),
            data.get("details"),
        )

    async def validate(self, config: dict) -> None:
        response = await self._post("validate", "/internal/config/validate", config)
        if response.status_code == 422:
            errors = response.json().get("errors") or ["invalid configuration"]
            self.metrics.config_validation_failures.inc()
            raise ConfigRejected(errors)
        if response.status_code != 200:
            raise CoreUnavailable()

    async def notify(self) -> None:
        try:
            await self._post("notify", "/internal/config/notify", {})
        except CoreUnavailable:
            log.warning("core was not notified about the configuration change; it picks it up on the next poll")

    async def identify(self, token: str) -> dict:
        response = await self._post("identify", "/internal/zabbix/identify", {"token": token})
        if response.status_code != 200:
            raise self._error(response)
        return response.json()

    async def preview(self, user: dict, sub: dict) -> dict:
        response = await self._post("preview", "/internal/preview", {"user": user, "subscription": sub})
        if response.status_code != 200:
            raise self._error(response)
        return response.json()

    async def send_now(self, user: dict, sub: dict) -> dict:
        response = await self._post("send_now", "/internal/send-now", {"user": user, "subscription": sub})
        if response.status_code != 200:
            raise self._error(response)
        return response.json()

    async def test_send(self, user: dict, sub: dict) -> None:
        response = await self._post("test_send", "/internal/test-send", {"user": user, "subscription": sub})
        if response.status_code != 200:
            raise self._error(response)

    async def send_login_link(self, email: str, url: str, ttl_minutes: int) -> None:
        body = {"email": email, "url": url, "ttl_minutes": ttl_minutes}
        response = await self._post("login_link", "/internal/mail/login-link", body)
        if response.status_code != 200:
            raise self._error(response)

    async def send_unsubscribed(self, email: str, title: str, manage_url: str) -> None:
        body = {"email": email, "title": title, "manage_url": manage_url}
        response = await self._post("unsubscribed", "/internal/mail/unsubscribed", body)
        if response.status_code != 200:
            raise self._error(response)

    async def info(self) -> dict:
        if self._info is not None and time.monotonic() - self._info_at < self.INFO_TTL:
            return self._info
        try:
            response = await self._http.get("/internal/info")
        except httpx.HTTPError as exc:
            self.metrics.core_failures.labels(operation="info").inc()
            log.warning("core request failed: info: %s", describe_request_error(exc))
            if self._info is not None:
                return self._info
            raise CoreUnavailable() from None
        if response.status_code != 200:
            self.metrics.core_failures.labels(operation="info").inc()
            log.warning("core request failed: info: %s", describe_response(response))
            if self._info is not None:
                return self._info
            raise CoreUnavailable()
        self._info = response.json()
        self._info_at = time.monotonic()
        return self._info
