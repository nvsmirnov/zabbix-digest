"""Zabbix user identity: token owner, status and email media."""

from dataclasses import dataclass, field

from ..errors import Category, PreparationError, ZabbixAuthError, ZabbixError, zabbix_to_preparation
from .client import ZabbixClient

GROUP_STATUS_DISABLED = "1"
MEDIA_ACTIVE = "0"


@dataclass
class ZabbixIdentity:
    userid: str
    username: str
    active: bool
    emails: list[str] = field(default_factory=list)

    def owns_email(self, email: str) -> bool:
        return email.strip().lower() in {e.lower() for e in self.emails}


def extract_emails(medias: list[dict]) -> list[str]:
    """Collect enabled email addresses from user media.

    Email media store "sendto" as a list since Zabbix 5.0. Other media types store a string;
    a string is accepted only when it looks like an email address.
    """
    result: list[str] = []
    seen: set[str] = set()
    for media in medias or []:
        if str(media.get("active", MEDIA_ACTIVE)) != MEDIA_ACTIVE:
            continue
        sendto = media.get("sendto")
        values = sendto if isinstance(sendto, list) else [sendto]
        for value in values:
            if not isinstance(value, str):
                continue
            value = value.strip()
            if "@" not in value or " " in value:
                continue
            if value.lower() not in seen:
                seen.add(value.lower())
                result.append(value)
    return result


async def get_identity(client: ZabbixClient, token: str) -> ZabbixIdentity:
    """Raises ZabbixError subclasses; ZabbixAuthError means the token is not accepted."""
    auth = await client.call("user.checkAuthentication", {"token": token})
    if not isinstance(auth, dict) or not auth.get("userid"):
        raise ZabbixAuthError("user.checkAuthentication returned no user")
    userid = str(auth["userid"])
    users = await client.call(
        "user.get",
        {
            "output": ["userid", "username"],
            "userids": [userid],
            "selectMedias": ["mediatypeid", "sendto", "active"],
            "selectUsrgrps": ["usrgrpid", "users_status"],
        },
        token=token,
    )
    if not users:
        return ZabbixIdentity(userid=userid, username=str(auth.get("username", "")), active=True, emails=[])
    user = users[0]
    groups = user.get("usrgrps") or []
    active = not any(str(g.get("users_status")) == GROUP_STATUS_DISABLED for g in groups)
    return ZabbixIdentity(
        userid=userid,
        username=str(user.get("username") or auth.get("username", "")),
        active=active,
        emails=extract_emails(user.get("medias") or []),
    )


async def validate_user(client: ZabbixClient, token: str, email: str) -> ZabbixIdentity:
    """Check token, user status and email ownership. Raises PreparationError."""
    try:
        identity = await get_identity(client, token)
    except ZabbixError as exc:
        raise zabbix_to_preparation(exc) from None
    if not identity.active:
        raise PreparationError(Category.ZABBIX_USER_DISABLED)
    if not identity.emails:
        raise PreparationError(
            Category.EMAIL_NOT_OWNED, "no enabled email media is visible for the Zabbix user"
        )
    if not identity.owns_email(email):
        raise PreparationError(Category.EMAIL_NOT_OWNED)
    return identity
