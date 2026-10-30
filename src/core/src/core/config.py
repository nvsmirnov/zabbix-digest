"""Core configuration."""

import os
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from common.durations import Duration
from common.http import proxy_summary
from common.logs import LogConfig
from common.retry import RetryConfig
from common.settings import StrictModel, YamlEnvSettings, load_settings
from common.smtp import SmtpConfig, smtp_target
from common.tls import trust_source


class ZabbixConfig(StrictModel):
    # Base URL of the Zabbix frontend, e.g. https://zabbix.example.com
    url: str = "http://localhost"
    timeout: Duration = 30.0
    verify_tls: bool = True
    # CA file for the Zabbix API. Default: SSL_CERT_FILE/SSL_CERT_DIR, then the certifi bundle.
    ca_file: str | None = None
    max_concurrent_requests: int = Field(default=10, ge=1)
    retry: RetryConfig = RetryConfig()
    # Upper bound of problems fetched per digest before limits are applied.
    max_problems: int = Field(default=5000, ge=1)
    # Accept widget fields this version does not know. Off by default: unknown fields may change filtering.
    ignore_unknown_widget_fields: bool = False

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value.rstrip("/")


class UsersSourceConfig(StrictModel):
    source: Literal["file", "http"] = "file"
    file: str = "users.yaml"
    cache_file: str = "cache/users.cache.yaml"
    poll_interval: Duration = 300.0
    startup_retry: RetryConfig = RetryConfig(attempts=5, initial_delay=2.0, max_delay=30.0)
    # Backoff cap while waiting for the first valid configuration without cache.
    wait_max_delay: Duration = 300.0
    request_timeout: Duration = 15.0


class FrontendConfig(StrictModel):
    # Internal URL Core uses to fetch configuration (http mode).
    url: str | None = None
    # Public URL used in unsubscribe links. Empty: links are not generated.
    public_url: str | None = None

    @field_validator("url", "public_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value.rstrip("/")


class HttpConfig(StrictModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = Field(default=8081, ge=1, le=65535)


class SchedulerConfig(StrictModel):
    # IANA name. Empty: TZ environment variable, then system local zone, then UTC.
    timezone: str | None = None
    tick_interval: Duration = 30.0
    max_workers: int = Field(default=5, ge=1)
    delivery_window: Duration = 7200.0
    retry_interval: Duration = 600.0
    cleanup_interval: Duration = 3600.0
    shutdown_timeout: Duration = 60.0

    @field_validator("delivery_window")
    @classmethod
    def _window(cls, value: float) -> float:
        if value <= 0 or value > 12 * 3600:
            raise ValueError("must be greater than 0 and at most 12h")
        return value


class TransportConfig(StrictModel):
    type: Literal["email", "external"]
    public: bool = False
    send_timeout: Duration = 30.0
    retry: RetryConfig = RetryConfig()
    settings: dict[str, Any] = {}


class MessagesConfig(StrictModel):
    header: str | None = None
    footer: str | None = None
    unsubscribe_text: str = "To unsubscribe from this digest, contact your Zabbix administrator."


def _default_transports() -> dict[str, TransportConfig]:
    return {"email": TransportConfig(type="email", public=True)}


class CoreSettings(YamlEnvSettings):
    zabbix: ZabbixConfig = ZabbixConfig()
    users: UsersSourceConfig = UsersSourceConfig()
    frontend: FrontendConfig = FrontendConfig()
    internal_api_key: str | None = None
    # Use proxies from the environment (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY, NO_PROXY):
    # proxy_use_internal for requests to the web frontend, proxy_use_zabbix for the Zabbix API.
    proxy_use_internal: bool = False
    proxy_use_zabbix: bool = True
    http: HttpConfig = HttpConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    state_dir: str = "state"
    transports: dict[str, TransportConfig] = Field(default_factory=_default_transports)
    smtp: SmtpConfig = SmtpConfig()
    messages: MessagesConfig = MessagesConfig()
    log: LogConfig = LogConfig()
    sentry_dsn: str | None = None
    sentry_environment: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "CoreSettings":
        if self.users.source == "http":
            if not self.frontend.url:
                raise ValueError("frontend.url is required when users.source is http")
            if not self.internal_api_key:
                raise ValueError("internal_api_key is required when users.source is http")
        if self.internal_api_key is not None and len(self.internal_api_key) < 16:
            raise ValueError("internal_api_key must be at least 16 characters")
        for name in self.transports:
            if not name or not all(c.isalnum() or c in "_-" for c in name):
                raise ValueError("transport names may contain only letters, digits, '_' and '-'")
        resolve_timezone(self.scheduler.timezone)
        return self


def resolve_timezone(name: str | None) -> ZoneInfo:
    candidates = [name] if name else []
    if not name:
        env_tz = os.environ.get("TZ", "").lstrip(":")
        if env_tz:
            candidates.append(env_tz)
        try:
            link = os.readlink("/etc/localtime")
            if "zoneinfo/" in link:
                candidates.append(link.split("zoneinfo/", 1)[1])
        except OSError:
            pass
        try:
            candidates.append(Path("/etc/timezone").read_text().strip())
        except OSError:
            pass
        candidates.append("UTC")
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            if name:
                raise ValueError(f"unknown time zone: {name}") from None
    return ZoneInfo("UTC")


def load_core_settings(path: str | None) -> CoreSettings:
    return load_settings(CoreSettings, path)


def settings_summary(settings: CoreSettings, tz: ZoneInfo) -> list[str]:
    """Main settings for the startup log. Never contains secrets."""
    users = settings.users
    if users.source == "file":
        source = f"file {users.file}"
    else:
        source = f"frontend {settings.frontend.url} (cache {users.cache_file})"
    transports = ", ".join(
        f"{name} ({t.type}{', public' if t.public else ''})" for name, t in sorted(settings.transports.items())
    )
    http = f"{settings.http.host}:{settings.http.port}" if settings.http.enabled else "disabled"
    return [
        f"zabbix: {settings.zabbix.url} (timeout {settings.zabbix.timeout:g}s, verify_tls {settings.zabbix.verify_tls}, "
        f"{'proxy from environment' if settings.proxy_use_zabbix else 'no proxy'}, "
        f"trusted CAs: {trust_source(settings.zabbix.ca_file)})",
        f"user configuration: {source}",
        f"public frontend URL: {settings.frontend.public_url or 'not set'}",
        f"state directory: {settings.state_dir}",
        f"time zone: {tz.key}, delivery window {settings.scheduler.delivery_window:g}s",
        f"transports: {transports}",
        f"smtp: {smtp_target(settings.smtp)}, from {settings.smtp.from_address}, "
        f"trusted CAs: {trust_source(settings.smtp.ca_file)}",
        f"http server: {http}, internal API {'enabled' if settings.internal_api_key else 'disabled'}",
        f"requests to the frontend: {'proxy from environment' if settings.proxy_use_internal else 'no proxy'}",
        f"proxy from environment: {proxy_summary()}",
    ]
