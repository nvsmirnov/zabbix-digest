"""User configuration model and validation.

Validation is syntactic only. Zabbix is never contacted here.
"""

import re
from typing import Any, Iterable

from pydantic import Field, ValidationError, field_validator

from common.settings import StrictModel, format_validation_error
from .zabbix.urls import WidgetUrlError, parse_widget_url
from .zabbix.widget import SEVERITY_NAMES, Overrides

ID_RE = re.compile(r"^[0-9a-f]{1,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
GENERATION_RE = re.compile(r"^[0-9a-f]{8,64}$")


class Subscription(StrictModel):
    id: str
    name: str | None = Field(default=None, max_length=200)
    widget_url: str = Field(max_length=2000)
    time: str
    transport: str = "email"
    enabled: bool = True
    # Filter overrides. They only narrow the widget filter; defaults keep the widget setting.
    # min_severity: severities >= this value (0..5).
    min_severity: int | None = None
    hide_acknowledged: bool = False
    hide_suppressed: bool = False
    unsubscribe_token: str | None = None
    # Adapter-specific recipient settings. Not used by the email adapter.
    transport_options: dict[str, Any] = {}

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not ID_RE.match(value):
            raise ValueError("must be 1-32 lowercase hexadecimal characters")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("time", mode="before")
    @classmethod
    def _time(cls, value: Any) -> str:
        # Unquoted YAML 1.1 values like 8:30 are parsed as base-60 integers (510).
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 24 * 60:
            value = f"{value // 60:02d}:{value % 60:02d}"
        if not isinstance(value, str):
            raise ValueError("must be a time in HH:MM format")
        value = value.strip()
        if re.match(r"^\d:[0-5]\d$", value):
            value = "0" + value
        if not TIME_RE.match(value):
            raise ValueError("must be a time in HH:MM format")
        return value

    @field_validator("min_severity", mode="before")
    @classmethod
    def _min_severity(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, str):
            names = [n.lower() for n in SEVERITY_NAMES]
            text = value.strip().lower()
            if text in names:
                return names.index(text)
            if text.isdigit():
                value = int(text)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < len(SEVERITY_NAMES):
            raise ValueError("must be a severity 0-5 or a severity name")
        return value

    @field_validator("widget_url")
    @classmethod
    def _widget_url(cls, value: str) -> str:
        try:
            parse_widget_url(value)
        except WidgetUrlError as exc:
            raise ValueError(str(exc)) from None
        return value.strip()

    @field_validator("unsubscribe_token")
    @classmethod
    def _unsub(cls, value: str | None) -> str | None:
        if value is not None and not TOKEN_RE.match(value):
            raise ValueError("must be 16-128 URL-safe characters")
        return value

    @property
    def overrides(self) -> Overrides:
        return Overrides(self.min_severity, self.hide_acknowledged, self.hide_suppressed)

    @property
    def title(self) -> str:
        return f"{self.name} (#{self.id})" if self.name else f"#{self.id}"


class User(StrictModel):
    email: str = Field(max_length=320)
    zabbix_token: str = Field(min_length=1, max_length=256)
    # Set by the frontend. Used to make sure a new token belongs to the same Zabbix user.
    zabbix_userid: str | None = None
    # Immutable per registration. Changes when a profile is deleted and created again.
    generation: str | None = None
    session_version: int = Field(default=1, ge=1)
    subscriptions: list[Subscription] = []

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip()
        if not EMAIL_RE.match(value):
            raise ValueError("must be an email address")
        return value

    @field_validator("zabbix_userid")
    @classmethod
    def _userid(cls, value: str | None) -> str | None:
        if value is not None and not str(value).isdigit():
            raise ValueError("must be numeric")
        return value

    @field_validator("generation")
    @classmethod
    def _generation(cls, value: str | None) -> str | None:
        if value is not None and not GENERATION_RE.match(value):
            raise ValueError("must be 8-64 lowercase hexadecimal characters")
        return value


class UsersConfig(StrictModel):
    revision: int = Field(default=0, ge=0)
    users: list[User] = []

    def iter_subscriptions(self) -> Iterable[tuple[User, Subscription]]:
        for user in self.users:
            for sub in user.subscriptions:
                yield user, sub

    def subscription_ids(self) -> set[str]:
        return {sub.id for _, sub in self.iter_subscriptions()}

    def find_subscription(self, subscription_id: str) -> tuple[User, Subscription] | None:
        for user, sub in self.iter_subscriptions():
            if sub.id == subscription_id:
                return user, sub
        return None

    def secrets(self) -> list[str]:
        values = []
        for user in self.users:
            values.append(user.zabbix_token)
            values.extend(s.unsubscribe_token for s in user.subscriptions if s.unsubscribe_token)
        return values


class UsersConfigError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_users_config(data: Any, transports: Iterable[str]) -> UsersConfig:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise UsersConfigError(["(root): must be a mapping"])
    try:
        config = UsersConfig.model_validate(data)
    except ValidationError as exc:
        raise UsersConfigError(format_validation_error(exc)) from None

    errors = []
    known_transports = set(transports)
    emails: dict[str, int] = {}
    ids: dict[str, str] = {}
    unsub: set[str] = set()
    generations: set[str] = set()
    for ui, user in enumerate(config.users):
        key = user.email.lower()
        if key in emails:
            errors.append(f"users.{ui}.email: duplicate email (also users.{emails[key]})")
        emails.setdefault(key, ui)
        if user.generation:
            if user.generation in generations:
                errors.append(f"users.{ui}.generation: duplicate value")
            generations.add(user.generation)
        for si, sub in enumerate(user.subscriptions):
            loc = f"users.{ui}.subscriptions.{si}"
            if sub.id in ids:
                errors.append(f"{loc}.id: duplicate subscription id {sub.id} (also {ids[sub.id]})")
            ids.setdefault(sub.id, loc)
            if sub.transport not in known_transports:
                errors.append(f"{loc}.transport: unknown transport '{sub.transport}'")
            if sub.unsubscribe_token:
                if sub.unsubscribe_token in unsub:
                    errors.append(f"{loc}.unsubscribe_token: duplicate value")
                unsub.add(sub.unsubscribe_token)
    if errors:
        raise UsersConfigError(errors)
    return config
