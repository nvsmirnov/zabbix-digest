"""Logging setup: JSON or text, stdout or rotating file, secret redaction."""

import logging
import logging.handlers
import os
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterable, Iterator, Literal

from pythonjsonlogger.json import JsonFormatter

from .settings import StrictModel

# user: email of the user the record is about; actor/target: audit (real user, impersonated user).
CONTEXT_FIELDS = (
    "component", "operation", "user", "subscription_id", "error_category", "transport", "actor", "target",
)

# Context fields of the current task: added to every record that does not set them itself.
_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add context fields (e.g. user, subscription_id) to all records inside the block."""
    token = _CONTEXT.set({**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def bind_log_context(**fields: Any) -> None:
    """Like log_context, until the end of the current task. For HTTP request handlers:
    every request runs in its own task, and tasks started from it inherit the fields."""
    _CONTEXT.set({**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}})


class LogConfig(StrictModel):
    level: str = "INFO"
    format: Literal["json", "text"] = "json"
    file: str | None = None
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    # One line per HTTP request (without health checks and /metrics).
    access: bool = True


class SecretRedactor(logging.Filter):
    """Replace known secret values in log records. Last line of defense only."""

    MIN_LEN = 6

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._static: set[str] = set()
        self._dynamic: set[str] = set()

    def add_static(self, values: Iterable[str | None]) -> None:
        with self._lock:
            self._static.update(v for v in values if v and len(v) >= self.MIN_LEN)

    def set_dynamic(self, values: Iterable[str | None]) -> None:
        new = {v for v in values if v and len(v) >= self.MIN_LEN}
        with self._lock:
            self._dynamic = new

    def redact(self, text: str) -> str:
        with self._lock:
            secrets = self._static | self._dynamic
        for secret in secrets:
            if secret in text:
                text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


REDACTOR = SecretRedactor()


class _ContextDefaults(logging.Filter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "component", None):
            record.component = self.component
        for name, value in _CONTEXT.get().items():
            if getattr(record, name, None) is None:
                setattr(record, name, value)
        return True


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        extras = []
        for name in CONTEXT_FIELDS:
            if name == "component":
                continue
            value = getattr(record, name, None)
            if value is not None:
                extras.append(f"{name}={value}")
        if extras:
            first, sep, rest = text.partition("\n")
            text = f"{first} [{' '.join(extras)}]{sep}{rest}"
        return text


def _json_formatter() -> logging.Formatter:
    return JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    )


def force_text_stdout() -> bool:
    return os.environ.get("LOG_FORCE_TEXT_STDOUT", "").strip().lower() in ("1", "true", "yes", "on")


def setup_logging(component: str, config: LogConfig) -> None:
    fmt = config.format
    file = config.file
    if force_text_stdout():
        fmt, file = "text", None
    if file:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            file, maxBytes=config.max_bytes, backupCount=config.backup_count, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TextFormatter() if fmt == "text" else _json_formatter())
    handler.addFilter(_ContextDefaults(component))
    handler.addFilter(REDACTOR)
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(config.level.upper())
    for noisy in ("httpx", "httpcore", "aiosmtplib", "mail.log"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for uv in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(uv)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
