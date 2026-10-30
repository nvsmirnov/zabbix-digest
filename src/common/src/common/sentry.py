"""Optional Sentry integration. Only unexpected errors are reported."""

import logging
from typing import Any

from .logs import REDACTOR

log = logging.getLogger(__name__)

_enabled = False

_SENSITIVE_KEYS = ("token", "password", "secret", "api_key", "apikey", "authorization", "cookie", "dsn", "zabbix_token")


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 10:
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and any(s in key.lower() for s in _SENSITIVE_KEYS):
                out[key] = "***"
            else:
                out[key] = _scrub(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_scrub(item, depth + 1) for item in value]
    if isinstance(value, str):
        return REDACTOR.redact(value)
    return value


def _before_send(event: dict, hint: dict) -> dict | None:
    event = _scrub(event)
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
    return event


def init_sentry(dsn: str | None, component: str, release: str, environment: str | None = None) -> None:
    global _enabled
    if not dsn:
        return
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=dsn,
        release=release,
        environment=environment,
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        before_send=_before_send,
        integrations=[LoggingIntegration(level=None, event_level=None)],
    )
    sentry_sdk.set_tag("component", component)
    _enabled = True
    log.info("sentry enabled")


def capture_exception(exc: BaseException) -> None:
    if not _enabled:
        return
    import sentry_sdk

    sentry_sdk.capture_exception(exc)
