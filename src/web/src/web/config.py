"""Web backend configuration."""

import json
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, field_validator
from pydantic_settings import NoDecode

from common.durations import Duration
from common.http import proxy_summary
from common.logs import LogConfig
from common.settings import StrictModel, YamlEnvSettings, load_settings
from common.tls import trust_source


def _email_list(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]
    return value


class RateLimit(StrictModel):
    limit: int = Field(ge=1)
    period: Duration


class RateLimitsConfig(StrictModel):
    login_link_per_ip: RateLimit = RateLimit(limit=10, period=3600)
    login_link_per_email: RateLimit = RateLimit(limit=3, period=3600)
    token_auth_per_ip: RateLimit = RateLimit(limit=20, period=3600)
    link_consume_per_ip: RateLimit = RateLimit(limit=30, period=3600)
    unsubscribe_per_ip: RateLimit = RateLimit(limit=30, period=3600)


class HttpConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)


class WebSettings(YamlEnvSettings):
    internal_api_key: str = Field(min_length=16)
    session_secret: str = Field(min_length=32)
    # Authoritative user configuration in http mode.
    users_file: str = "users.yaml"
    # One-time login link bookkeeping.
    state_dir: str = "web-state"
    core_url: str = "http://localhost:8081"
    core_timeout: Duration = 60.0
    # Use proxies from the environment (HTTP_PROXY etc.) for requests to Core.
    proxy_use_internal: bool = False
    # Public base URL of this application, used in emailed links.
    public_url: str = "http://localhost:8080"
    # Link to user help, shown in the UI. Empty: no link.
    help_url: str | None = None
    admin_emails: Annotated[list[str], NoDecode, BeforeValidator(_email_list)] = []
    # 0 disables the limit.
    max_subscriptions_per_user: int = Field(default=20, ge=0)
    subscription_id_length: int = Field(default=4, ge=1, le=32)
    login_link_ttl: Duration = 900.0
    impersonation_ttl: Duration = 3600.0
    session_max_age: Duration = 400 * 86400.0
    cookie_secure: bool = True
    # Use X-Forwarded-For for client IPs. Enable only behind a trusted reverse proxy.
    trust_proxy_headers: bool = False
    rate_limits: RateLimitsConfig = RateLimitsConfig()
    http: HttpConfig = HttpConfig()
    # Compiled Vue application. Empty: the UI is not served.
    static_dir: str | None = "src/web/ui/dist"
    log: LogConfig = LogConfig()
    sentry_dsn: str | None = None
    sentry_browser_dsn: str | None = None
    sentry_environment: str | None = None

    @field_validator("core_url", "public_url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("help_url")
    @classmethod
    def _help_url(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if not value:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value

    @field_validator("admin_emails")
    @classmethod
    def _lower(cls, value: list[str]) -> list[str]:
        return [v.strip().lower() for v in value if v.strip()]


def load_web_settings(path: str | None) -> WebSettings:
    return load_settings(WebSettings, path)


def settings_summary(settings: WebSettings) -> list[str]:
    """Main settings for the startup log. Never contains secrets."""
    return [
        f"core: {settings.core_url} (timeout {settings.core_timeout:g}s, "
        f"{'proxy from environment' if settings.proxy_use_internal else 'no proxy'}, trusted CAs: {trust_source()})",
        f"public URL: {settings.public_url}, secure cookies {settings.cookie_secure}",
        f"help URL: {settings.help_url or 'not set'}",
        f"user configuration file: {settings.users_file}",
        f"state directory: {settings.state_dir}",
        f"http server: {settings.http.host}:{settings.http.port}, trust proxy headers {settings.trust_proxy_headers}",
        f"static UI: {settings.static_dir or 'not served'}",
        f"administrators: {len(settings.admin_emails)}",
        f"proxy from environment: {proxy_summary()}",
    ]
