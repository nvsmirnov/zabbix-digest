"""Adapter interface, named transport instances and delivery with retries.

A built-in adapter is a class with:
  - __init__(name, settings: dict, core_settings)  -- validate settings, raise ValueError on errors.
  - async send(message: dict) -> None              -- render and deliver; raise TransportError,
                                                      PermanentTransportError or RenderError.
  - supports_preview: bool
  - render_preview(message: dict) -> dict          -- {"content_type", "subject", "body"}.
Register new adapter types in ADAPTER_TYPES.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from common.errors import describe_error
from common.sentry import capture_exception
from ..config import CoreSettings, TransportConfig
from ..errors import PermanentTransportError, PreviewNotSupported, RenderError, TransportError

log = logging.getLogger(__name__)


class Adapter(Protocol):
    supports_preview: bool

    async def send(self, message: dict) -> None: ...

    def render_preview(self, message: dict) -> dict: ...


@dataclass
class Transport:
    name: str
    config: TransportConfig
    adapter: Adapter


class DeliveryFailed(Exception):
    def __init__(self, reason: str, permanent: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent


class TransportRegistry:
    def __init__(self, transports: dict[str, Transport], metrics: Any = None) -> None:
        self.transports = transports
        self.metrics = metrics

    def names(self) -> list[str]:
        return list(self.transports)

    def get(self, name: str) -> Transport:
        return self.transports[name]

    def describe(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "type": t.config.type,
                "public": t.config.public,
                "preview": bool(getattr(t.adapter, "supports_preview", False)),
            }
            for t in self.transports.values()
        ]

    def render_preview(self, name: str, message: dict) -> dict:
        transport = self.get(name)
        if not getattr(transport.adapter, "supports_preview", False):
            raise PreviewNotSupported(name)
        return transport.adapter.render_preview(message)

    async def deliver(
        self,
        name: str,
        message: dict,
        stop_retrying: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        """Deliver with retries. Raises RenderError or DeliveryFailed."""
        transport = self.get(name)
        cfg = transport.config
        attempt = 1
        while True:
            started = time.monotonic()
            try:
                await asyncio.wait_for(transport.adapter.send(message), timeout=cfg.send_timeout)
                self._observe(name, message, "success", time.monotonic() - started)
                return
            except RenderError:
                raise
            except PermanentTransportError as exc:
                if self.metrics:
                    self.metrics.delivery_attempt_failures.labels(transport=name).inc()
                self._observe(name, message, "failure", time.monotonic() - started)
                raise DeliveryFailed(str(exc), permanent=True) from None
            except asyncio.TimeoutError:
                reason = f"timed out after {cfg.send_timeout:g}s"
            except TransportError as exc:
                reason = str(exc)
            except OSError as exc:
                reason = describe_error(exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("unexpected transport error", extra={**_ids(message), "transport": name, "operation": "deliver"})
                capture_exception(exc)
                self._observe(name, message, "failure", time.monotonic() - started)
                raise DeliveryFailed(f"unexpected error: {describe_error(exc)}", permanent=True) from None
            if self.metrics:
                self.metrics.delivery_attempt_failures.labels(transport=name).inc()
            extra = {**_ids(message), "transport": name, "operation": "deliver"}
            if attempt >= cfg.retry.attempts or stop_retrying():
                log.warning("delivery attempt %d/%d failed: %s", attempt, cfg.retry.attempts, reason, extra=extra)
                if attempt >= cfg.retry.attempts:
                    log.error("delivery failed after %d attempts, giving up", attempt, extra=extra)
                else:
                    log.error("delivery failed, not retrying because of shutdown", extra=extra)
                self._observe(name, message, "failure", time.monotonic() - started)
                raise DeliveryFailed(reason, permanent=False)
            delay = cfg.retry.delay(attempt)
            log.warning(
                "delivery attempt %d/%d failed, retrying in %.1fs: %s",
                attempt, cfg.retry.attempts, delay, reason, extra=extra,
            )
            if self.metrics:
                self.metrics.transport_retries.labels(transport=name).inc()
            await sleep(delay)
            attempt += 1

    def _observe(self, name: str, message: dict, result: str, duration: float) -> None:
        if not self.metrics:
            return
        self.metrics.deliveries.labels(transport=name, type=message["type"], result=result).inc()
        self.metrics.delivery_duration.labels(transport=name).observe(duration)


def adapter_types() -> dict[str, type]:
    from .email import EmailAdapter
    from .external import ExternalAdapter

    return {"email": EmailAdapter, "external": ExternalAdapter}


def build_registry(settings: CoreSettings, metrics: Any = None) -> TransportRegistry:
    """Raises ValueError with a safe message when a transport is misconfigured."""
    types = adapter_types()
    transports = {}
    for name, cfg in settings.transports.items():
        cls = types.get(cfg.type)
        if cls is None:
            raise ValueError(f"transport '{name}': unknown adapter type '{cfg.type}'")
        try:
            adapter = cls(name, cfg.settings, settings)
        except ValueError as exc:
            raise ValueError(f"transport '{name}': {exc}") from None
        transports[name] = Transport(name=name, config=cfg, adapter=adapter)
    return TransportRegistry(transports, metrics)


def _ids(message: dict) -> dict:
    """Log context of a message: recipient and subscription."""
    return {"user": message["recipient"]["email"], "subscription_id": message["subscription"]["id"]}
