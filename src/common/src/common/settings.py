"""YAML + environment settings loading.

Precedence: environment variables > YAML values > defaults.
Nested keys map to environment variables with "__", e.g. SMTP__HOST -> smtp.host.
"""

import os
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .yamlio import load_yaml


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class YamlEnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # YAML values are passed as init kwargs, so env must come first to win.
        return (env_settings, init_settings)


class SettingsError(Exception):
    """Configuration could not be loaded. The message is safe to print."""


S = TypeVar("S", bound=YamlEnvSettings)


def format_validation_error(exc: ValidationError) -> list[str]:
    """Return error lines without input values (they may contain secrets)."""
    lines = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(part) for part in err.get("loc", ())) or "(root)"
        lines.append(f"{loc}: {err.get('msg', 'invalid value')}")
    return lines


def load_settings(cls: type[S], path: str | None) -> S:
    data: dict[str, Any] = {}
    if path:
        if not os.path.exists(path):
            raise SettingsError(f"configuration file not found: {path}")
        try:
            loaded = load_yaml(path)
        except Exception as exc:  # noqa: BLE001
            raise SettingsError(f"cannot parse configuration file {path}: {exc}") from None
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise SettingsError(f"configuration file {path} must contain a mapping")
        data = loaded
    try:
        return cls(**data)
    except ValidationError as exc:
        raise SettingsError("invalid configuration:\n  " + "\n  ".join(format_validation_error(exc))) from None
