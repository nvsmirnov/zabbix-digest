"""SMTP settings and sending."""

import ssl
from email.message import EmailMessage
from typing import Literal

import aiosmtplib
from pydantic import Field

from .durations import Duration
from .settings import StrictModel
from .tls import ssl_context


class SmtpConfig(StrictModel):
    host: str = "localhost"
    port: int = Field(default=25, ge=1, le=65535)
    # none: plain SMTP; starttls: upgrade with STARTTLS (required); tls: implicit TLS (SMTPS, port 465).
    security: Literal["none", "starttls", "tls"] = "none"
    username: str | None = None
    password: str | None = None
    verify_tls: bool = True
    ca_file: str | None = None
    timeout: Duration = 30.0
    from_address: str = "zabbix-digest@localhost"
    local_hostname: str | None = None


def smtp_target(config: SmtpConfig) -> str:
    """Server description for logs, e.g. "smtp.example.com:587 (starttls)"."""
    return f"{config.host}:{config.port} ({config.security})"


class SmtpPermanentError(Exception):
    """SMTP failure that retrying will not fix (rejected recipient, auth failure)."""


def _tls_context(config: SmtpConfig) -> ssl.SSLContext:
    return ssl_context(config.verify_tls, config.ca_file)


def check_tls(config: SmtpConfig) -> None:
    """Fail at startup instead of on every send. Raises ValueError."""
    if config.security != "none":
        _tls_context(config)


async def send_email(config: SmtpConfig, message: EmailMessage) -> None:
    kwargs = dict(
        hostname=config.host,
        port=config.port,
        timeout=config.timeout,
        local_hostname=config.local_hostname,
        username=config.username or None,
        password=config.password or None,
    )
    if config.security == "tls":
        kwargs.update(use_tls=True, tls_context=_tls_context(config))
    elif config.security == "starttls":
        kwargs.update(start_tls=True, tls_context=_tls_context(config))
    else:
        kwargs.update(start_tls=False, use_tls=False)
    try:
        await aiosmtplib.send(message, **kwargs)
    except aiosmtplib.SMTPAuthenticationError as exc:
        raise SmtpPermanentError(f"SMTP authentication failed on {smtp_target(config)} (code {exc.code})") from None
    except aiosmtplib.SMTPRecipientsRefused as exc:
        codes = sorted({r.code for r in exc.recipients})
        if all(500 <= c < 600 for c in codes):
            raise SmtpPermanentError(f"SMTP recipient refused by {smtp_target(config)} (codes {codes})") from None
        raise
    except aiosmtplib.SMTPResponseException as exc:
        if 500 <= exc.code < 600:
            raise SmtpPermanentError(
                f"SMTP permanent error from {smtp_target(config)} (code {exc.code}: {' '.join(exc.message.split())[:200]})"
            ) from None
        raise
