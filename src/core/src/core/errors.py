"""Error categories and exception types."""


class Category:
    ZABBIX_AUTH = "zabbix_auth"
    ZABBIX_USER_DISABLED = "zabbix_user_disabled"
    EMAIL_NOT_OWNED = "email_not_owned"
    WIDGET_URL = "widget_url"
    WIDGET_NOT_FOUND = "widget_not_found"
    WIDGET_UNSUPPORTED = "widget_unsupported"
    ZABBIX_UNAVAILABLE = "zabbix_unavailable"
    ZABBIX_API = "zabbix_api"
    RENDER = "render"
    INTERNAL = "internal"


# Simple English explanations for users. Details are added separately and must be safe.
USER_MESSAGES = {
    Category.ZABBIX_AUTH: (
        "The Zabbix API token is invalid, expired or revoked, or the Zabbix user cannot log in. "
        "Create a new API token in Zabbix and update your profile."
    ),
    Category.ZABBIX_USER_DISABLED: "Your Zabbix user is disabled. Contact your Zabbix administrator.",
    Category.EMAIL_NOT_OWNED: (
        "The email address of this subscription is no longer configured as media for your Zabbix user."
    ),
    Category.WIDGET_URL: "The Problems widget link of this subscription is not supported.",
    Category.WIDGET_NOT_FOUND: "The Problems widget of this subscription was not found or is not accessible.",
    Category.WIDGET_UNSUPPORTED: "The Problems widget uses settings that the digest service does not support.",
    Category.ZABBIX_UNAVAILABLE: "Zabbix was temporarily unavailable. The report could not be prepared.",
    Category.ZABBIX_API: "Zabbix returned an error while the report was prepared.",
    Category.RENDER: "The report could not be rendered.",
    Category.INTERNAL: "An internal error occurred while the report was prepared.",
}

class PreparationError(Exception):
    """Report could not be prepared. `details` must not contain secrets."""

    def __init__(self, category: str, details: str | None = None) -> None:
        super().__init__(f"{category}: {details}" if details else category)
        self.category = category
        self.details = details

    @property
    def user_message(self) -> str:
        return USER_MESSAGES.get(self.category, USER_MESSAGES[Category.INTERNAL])


class ZabbixError(Exception):
    """Base for Zabbix API errors. Messages never contain tokens."""


class ZabbixTemporaryError(ZabbixError):
    pass


class ZabbixAuthError(ZabbixError):
    pass


class ZabbixApiError(ZabbixError):
    pass


def zabbix_to_preparation(exc: ZabbixError) -> PreparationError:
    if isinstance(exc, ZabbixAuthError):
        return PreparationError(Category.ZABBIX_AUTH)
    if isinstance(exc, ZabbixTemporaryError):
        return PreparationError(Category.ZABBIX_UNAVAILABLE, str(exc))
    return PreparationError(Category.ZABBIX_API, str(exc))


class TransportError(Exception):
    """Delivery failed; may be retried."""


class PermanentTransportError(TransportError):
    """Delivery failed; retrying will not help."""


class RenderError(Exception):
    """Adapter could not render a message."""


class PreviewNotSupported(Exception):
    pass
