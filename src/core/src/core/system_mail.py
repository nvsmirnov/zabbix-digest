"""System emails requested by the web frontend: login links and unsubscribe confirmations.

Sent directly through the global smtp section, not through transports: a login link
must go to the email address itself.
"""

import html
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from common.smtp import SmtpConfig, check_tls, send_email


def _message(config: SmtpConfig, to: str, subject: str, text: str, html_body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = config.from_address
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=config.from_address.rsplit("@", 1)[-1] or None)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    return msg


def _html(paragraphs: list[str], link: tuple[str, str] | None = None) -> str:
    parts = [f"<p>{html.escape(p)}</p>" for p in paragraphs]
    if link:
        parts.insert(1, f'<p><a href="{html.escape(link[1])}">{html.escape(link[0])}</a></p>')
    body = "".join(parts)
    return f'<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;font-size:14px;">{body}</body></html>'


class SystemMailer:
    def __init__(self, config: SmtpConfig) -> None:
        check_tls(config)
        self.config = config

    async def send_login_link(self, to: str, url: str, ttl_minutes: int) -> None:
        text = (
            "Use this link to log in to Zabbix Digest:\n\n"
            f"{url}\n\n"
            f"The link is valid for {ttl_minutes} minutes and can be used once.\n"
            "If you did not request it, ignore this message.\n"
        )
        body = _html(
            [
                "Use this link to log in to Zabbix Digest:",
                f"The link is valid for {ttl_minutes} minutes and can be used once.",
                "If you did not request it, ignore this message.",
            ],
            ("Log in to Zabbix Digest", url),
        )
        await send_email(self.config, _message(self.config, to, "Zabbix Digest login link", text, body))

    async def send_unsubscribed(self, to: str, title: str, manage_url: str) -> None:
        text = (
            f"You have been unsubscribed from the digest {title}.\n\n"
            "The subscription is disabled, not deleted. You can enable it again here:\n"
            f"{manage_url}\n"
        )
        body = _html(
            [
                f"You have been unsubscribed from the digest {title}.",
                "The subscription is disabled, not deleted. You can enable it again in Zabbix Digest.",
            ],
            ("Open Zabbix Digest", manage_url),
        )
        await send_email(self.config, _message(self.config, to, f"Unsubscribed: {title}", text, body))
