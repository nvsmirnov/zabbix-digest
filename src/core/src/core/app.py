"""Core service assembly and lifecycle."""

import asyncio
import contextlib
import logging
import signal
from typing import Iterator

import uvicorn

from common.version import __version__
from common.logs import REDACTOR
from common.sentry import capture_exception
from .config import CoreSettings, resolve_timezone, settings_summary
from .metrics import CoreMetrics
from .processing import Processor
from .scheduler import Scheduler
from .source import make_source
from .state import StateStore
from .system_mail import SystemMailer
from .transports.base import build_registry
from .zabbix.client import ZabbixClient

log = logging.getLogger(__name__)


class _Server(uvicorn.Server):
    """uvicorn server that leaves signal handling to CoreApp."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class CoreApp:
    def __init__(self, settings: CoreSettings, zabbix_transport=None, source=None) -> None:
        self.settings = settings
        self.tz = resolve_timezone(settings.scheduler.timezone)
        self.metrics = CoreMetrics()
        REDACTOR.add_static([settings.internal_api_key, settings.smtp.password])
        self.transports = build_registry(settings, self.metrics)
        for t in self.transports.transports.values():
            smtp = getattr(t.adapter, "smtp", None)
            if smtp is not None:
                REDACTOR.add_static([smtp.password])
        self.zabbix = ZabbixClient(
            settings.zabbix.url,
            timeout=settings.zabbix.timeout,
            verify_tls=settings.zabbix.verify_tls,
            ca_file=settings.zabbix.ca_file,
            max_concurrent=settings.zabbix.max_concurrent_requests,
            retry=settings.zabbix.retry,
            transport=zabbix_transport,
            on_error=lambda kind: self.metrics.zabbix_errors.labels(kind=kind).inc(),
            on_retry=self.metrics.zabbix_retries.inc,
            use_proxy=settings.proxy_use_zabbix,
        )
        self.state = StateStore(settings.state_dir)
        self.mailer = SystemMailer(settings.smtp)
        self.source = source or make_source(settings, self.transports.names(), self.metrics)
        self.processor = Processor(settings, self.tz, self.zabbix, self.transports, self.state, self.metrics)
        self.scheduler = Scheduler(settings.scheduler, self.tz, self.source, self.state, self.processor, self.metrics)
        self.stop = asyncio.Event()
        self._scheduler_started = False
        self._server: _Server | None = None

    def ready(self) -> bool:
        return self.source.current is not None and self._scheduler_started and not self.stop.is_set()

    async def aclose(self) -> None:
        await self.zabbix.aclose()
        await self.source.aclose()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError):
                pass

    def _on_signal(self, sig: signal.Signals) -> None:
        if not self.stop.is_set():
            log.info("received %s, shutting down", sig.name)
            self.stop.set()

    async def _periodic(self, interval: float, func, name: str) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                result = func()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                self.metrics.unexpected_errors.inc()
                log.exception("%s failed", name)
                capture_exception(exc)

    async def run(self) -> None:
        log.info("zabbix digest core %s starting", __version__)
        self.log_settings()
        self.install_signal_handlers()
        server_task = None
        if self.settings.http.enabled:
            server_task = asyncio.create_task(self._serve_http())
        try:
            if not await self.source.start(self.stop):
                return
            background = [
                asyncio.create_task(self._periodic(self.settings.scheduler.cleanup_interval, self.scheduler.cleanup, "state cleanup")),
            ]
            if self.settings.users.source == "http":
                background.append(
                    asyncio.create_task(self._periodic(self.settings.users.poll_interval, self.source.refresh, "config refresh"))
                )
            self.scheduler.cleanup()
            self._scheduler_started = True
            await self.scheduler.run(self.stop)
            for task in background:
                task.cancel()
            await self.scheduler.shutdown(self.settings.scheduler.shutdown_timeout)
        finally:
            self._scheduler_started = False
            if server_task is not None:
                if self._server is not None:
                    self._server.should_exit = True
                else:
                    server_task.cancel()
                try:
                    await asyncio.wait_for(server_task, timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            await self.aclose()
            log.info("core stopped")

    async def _serve_http(self) -> None:
        from .api import create_app

        config = uvicorn.Config(
            create_app(self),
            host=self.settings.http.host,
            port=self.settings.http.port,
            log_config=None,
            access_log=False,
            lifespan="off",
        )
        self._server = _Server(config)
        try:
            await self._server.serve()
        except SystemExit:
            # uvicorn exits when the port cannot be bound.
            log.error("HTTP server failed to start on %s:%d", self.settings.http.host, self.settings.http.port)
            self.stop.set()

    def log_settings(self) -> None:
        for line in settings_summary(self.settings, self.tz):
            log.info("config: %s", line)

    async def run_once(self, only: set[str] | None = None, force: bool = False) -> dict[str, str]:
        """Process subscriptions once and wait for them. Returns {subscription_id: result}.

        Without force: one scheduler pass over due subscriptions, state is updated.
        With force: all enabled subscriptions are sent now, state is not changed.
        """
        self.log_settings()
        if not await self.source.start(self.stop):
            return {}
        if force:
            return await self.scheduler.force(only)
        tasks = self.scheduler.tick(only=only)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            task.get_name()[len("subscription-"):]: result if isinstance(result, str) else "error"
            for task, result in zip(tasks, results)
        }
