"""Built-in email adapter: HTML body rendered with Jinja2, sent over SMTP."""

import html
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

import aiosmtplib
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, TemplateError, select_autoescape
from pydantic import ValidationError

from common.errors import describe_error
from common.settings import StrictModel, format_validation_error
from common.smtp import SmtpConfig, SmtpPermanentError, check_tls, send_email, smtp_target
from ..config import CoreSettings
from ..errors import PermanentTransportError, RenderError, TransportError
from ..messages import subject_for

TEMPLATES = {"digest": "digest.html.j2", "error": "error.html.j2", "test": "test.html.j2"}


class EmailSettings(StrictModel):
    # Overrides smtp.from_address.
    from_address: str | None = None
    subject_prefix: str = ""
    # Directory with custom templates; missing files fall back to built-in ones.
    template_dir: str | None = None
    # Overrides the global smtp section for this transport instance.
    smtp: SmtpConfig | None = None


def make_environment(template_dir: str | None) -> Environment:
    loaders = []
    if template_dir:
        if not Path(template_dir).is_dir():
            raise ValueError(f"template_dir does not exist: {template_dir}")
        loaders.append(FileSystemLoader(template_dir))
    loaders.append(PackageLoader("core.transports", "templates"))
    env = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=select_autoescape(enabled_extensions=("html", "j2"), default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


def html_to_text(body: str) -> str:
    """Plain text part. One table row per line; the table header row is dropped,
    so the text (often used for previews and notifications) starts with the data."""
    text = re.sub(r"(?is)<(script|style|head|thead)\b.*?</\1>", "", body)
    text = re.sub(r"\s+", " ", text)  # whitespace in HTML source is not significant
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li|table)>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", " | ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip().rstrip("|").strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line) + "\n"


class EmailAdapter:
    supports_preview = True

    def __init__(self, name: str, settings: dict, core: CoreSettings) -> None:
        try:
            self.settings = EmailSettings.model_validate(settings or {})
        except ValidationError as exc:
            raise ValueError("; ".join(format_validation_error(exc))) from None
        self.name = name
        self.smtp = self.settings.smtp or core.smtp
        self.from_address = self.settings.from_address or self.smtp.from_address
        check_tls(self.smtp)
        self.env = make_environment(self.settings.template_dir)

    def render(self, message: dict) -> tuple[str, str]:
        template_name = TEMPLATES.get(message.get("type", ""))
        if template_name is None:
            raise RenderError(f"unknown message type {message.get('type')!r}")
        subject = self.settings.subject_prefix + subject_for(message)
        try:
            body = self.env.get_template(template_name).render(m=message, subject=subject)
        except TemplateError as exc:
            raise RenderError(f"template error: {type(exc).__name__}: {exc}") from None
        return subject, body

    def render_preview(self, message: dict) -> dict:
        subject, body = self.render(message)
        return {"content_type": "text/html", "subject": subject, "body": body}

    def build(self, message: dict) -> EmailMessage:
        subject, body = self.render(message)
        email = EmailMessage()
        email["From"] = self.from_address
        email["To"] = message["recipient"]["email"]
        email["Subject"] = subject
        email["Date"] = formatdate(localtime=True)
        email["Message-ID"] = make_msgid(domain=self.from_address.rsplit("@", 1)[-1] or None)
        email["Auto-Submitted"] = "auto-generated"
        unsub = message.get("unsubscribe") or {}
        if unsub.get("url"):
            email["List-Unsubscribe"] = f"<{unsub['url']}>"
        email.set_content(html_to_text(body))
        email.add_alternative(body, subtype="html")
        return email

    async def send(self, message: dict) -> None:
        email = self.build(message)
        try:
            await send_email(self.smtp, email)
        except SmtpPermanentError as exc:
            raise PermanentTransportError(str(exc)) from None
        except aiosmtplib.SMTPException as exc:
            raise TransportError(f"SMTP error on {smtp_target(self.smtp)}: {describe_error(exc)}") from None
