"""User configuration sources: local file or frontend HTTP API with a persistent cache."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable

import httpx

from common.errors import describe_error
from common.http import describe_request_error, describe_response, proxy_options
from common.tls import ssl_context
from common.logs import REDACTOR
from common.yamlio import atomic_write_yaml, load_yaml
from .config import CoreSettings
from .metrics import CoreMetrics
from .users import UsersConfig, UsersConfigError, validate_users_config

log = logging.getLogger(__name__)

API_KEY_HEADER = "X-Internal-Api-Key"


class SourceError(Exception):
    pass


class UsersSource:
    """Holds the latest accepted configuration. Subclasses implement loading."""

    def __init__(self, settings: CoreSettings, transports: list[str], metrics: CoreMetrics) -> None:
        self.settings = settings
        self.transports = transports
        self.metrics = metrics
        self.current: UsersConfig | None = None
        self.listeners: list[Callable[[UsersConfig | None, UsersConfig], None]] = []
        self._lock = asyncio.Lock()

    def validate(self, data: Any, source: str) -> UsersConfig:
        try:
            return validate_users_config(data, self.transports)
        except UsersConfigError:
            self.metrics.config_validation_failures.labels(source=source).inc()
            raise

    def _apply(self, config: UsersConfig, origin: str) -> bool:
        old = self.current
        if old is not None and old == config:
            return False
        if old is not None and config.revision < old.revision:
            log.warning("applying user configuration with a lower revision (%d < %d)", config.revision, old.revision)
        self.current = config
        REDACTOR.set_dynamic(config.secrets())
        self.metrics.config_revision.set(config.revision)
        log.info(
            "user configuration applied (revision %d, %d users, %d subscriptions, from %s)",
            config.revision, len(config.users), len(config.subscription_ids()), origin,
        )
        for listener in self.listeners:
            listener(old, config)
        return True

    async def start(self, stop: asyncio.Event) -> bool:
        """Load the initial configuration. Returns False if stopped before one became available."""
        raise NotImplementedError

    async def refresh(self) -> bool:
        """Reload from the source. Returns True when a new configuration was applied."""
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class FileSource(UsersSource):
    def __init__(self, settings: CoreSettings, transports: list[str], metrics: CoreMetrics) -> None:
        super().__init__(settings, transports, metrics)
        self.path = Path(settings.users.file)

    def load(self) -> UsersConfig:
        if not self.path.exists():
            raise SourceError(f"user configuration file not found: {self.path}")
        try:
            data = load_yaml(self.path)
        except Exception as exc:  # noqa: BLE001
            self.metrics.config_validation_failures.labels(source="file").inc()
            raise SourceError(f"cannot parse {self.path}: {type(exc).__name__}: {exc}") from None
        return self.validate(data, "file")

    async def start(self, stop: asyncio.Event) -> bool:
        # The file is read once at startup; changes require a restart.
        # Missing or invalid file is fatal in file mode.
        self._apply(self.load(), str(self.path))
        _warn_permissions(self.path)
        return True

    async def refresh(self) -> bool:
        return False


class HttpSource(UsersSource):
    def __init__(
        self,
        settings: CoreSettings,
        transports: list[str],
        metrics: CoreMetrics,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        super().__init__(settings, transports, metrics)
        self.cache_path = Path(settings.users.cache_file)
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=settings.frontend.url or "",
            timeout=settings.users.request_timeout,
            headers={API_KEY_HEADER: settings.internal_api_key or ""},
            transport=transport,
            verify=ssl_context(),
            **proxy_options(settings.proxy_use_internal),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch(self) -> UsersConfig:
        try:
            response = await self._http.get("/internal/config")
        except httpx.HTTPError as exc:
            raise SourceError(f"frontend request failed: {describe_request_error(exc)}") from None
        if response.status_code != 200:
            raise SourceError(f"frontend request failed: {describe_response(response)}")
        try:
            data = response.json()
        except ValueError:
            raise SourceError(f"frontend returned invalid JSON: {describe_response(response)}") from None
        return self.validate(data, "http")

    def load_cache(self) -> UsersConfig | None:
        if not self.cache_path.exists():
            return None
        try:
            return self.validate(load_yaml(self.cache_path), "cache")
        except Exception as exc:  # noqa: BLE001
            log.error("cached user configuration is not usable: %s", exc)
            return None

    def save_cache(self, config: UsersConfig) -> None:
        atomic_write_yaml(self.cache_path, config.model_dump(mode="json", exclude_defaults=False), mode=0o600)

    def _accept(self, config: UsersConfig, origin: str) -> bool:
        changed = self._apply(config, origin)
        if changed:
            try:
                self.save_cache(config)
            except OSError as exc:
                log.error("cannot write configuration cache: %s", exc)
        return changed

    async def start(self, stop: asyncio.Event) -> bool:
        retry = self.settings.users.startup_retry
        for attempt in range(1, retry.attempts + 1):
            try:
                self._accept(await self.fetch(), "frontend")
                return True
            except (SourceError, UsersConfigError) as exc:
                self.metrics.config_sync_failures.inc()
                log.warning("startup configuration fetch failed (attempt %d/%d): %s", attempt, retry.attempts, exc)
            if attempt < retry.attempts and await _wait(stop, retry.delay(attempt), self._sleep):
                return False
        cached = self.load_cache()
        if cached is not None:
            log.warning("frontend unavailable, starting with cached configuration (revision %d)", cached.revision)
            self._apply(cached, "cache")
            return True
        log.error("no valid configuration available; digest scheduling waits for the frontend")
        attempt = 0
        while True:
            attempt += 1
            delay = min(self.settings.users.wait_max_delay, retry.delay(retry.attempts + attempt))
            if await _wait(stop, delay, self._sleep):
                return False
            if self.current is not None:
                return True  # applied through a push notification meanwhile
            try:
                self._accept(await self.fetch(), "frontend")
                return True
            except (SourceError, UsersConfigError) as exc:
                self.metrics.config_sync_failures.inc()
                log.warning("configuration fetch failed: %s", exc)

    async def refresh(self) -> bool:
        async with self._lock:
            try:
                config = await self.fetch()
            except (SourceError, UsersConfigError) as exc:
                self.metrics.config_sync_failures.inc()
                log.warning("configuration fetch failed, keeping the current one: %s", exc)
                return False
            return self._accept(config, "frontend")


async def _wait(stop: asyncio.Event, delay: float, sleep: Callable[[float], Any]) -> bool:
    """Sleep up to `delay`. Returns True if stop was requested."""
    if stop.is_set():
        return True
    sleeper = asyncio.ensure_future(sleep(delay))
    stopper = asyncio.ensure_future(stop.wait())
    done, pending = await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    return stop.is_set()


def _warn_permissions(path: Path) -> None:
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        log.warning("user configuration file %s is accessible by other users (mode %o); use 0600", path, mode)


def make_source(settings: CoreSettings, transports: list[str], metrics: CoreMetrics) -> UsersSource:
    if settings.users.source == "http":
        return HttpSource(settings, transports, metrics)
    return FileSource(settings, transports, metrics)
