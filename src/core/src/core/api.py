"""Core HTTP API: health, metrics and authenticated /internal/* endpoints."""

import hmac
import logging
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError

from common.access import log_access
from common.logs import bind_log_context
from common.errors import describe_error
from common.smtp import smtp_target
from common.version import __version__
from common.settings import format_validation_error
from .errors import PreparationError, PreviewNotSupported, RenderError, ZabbixAuthError, ZabbixError, ZabbixTemporaryError
from .messages import SCHEMA_VERSION
from .processing import ShuttingDown
from .source import API_KEY_HEADER
from .transports.base import DeliveryFailed
from .users import Subscription, User, UsersConfigError
from .zabbix.identity import get_identity

log = logging.getLogger(__name__)
access_log = logging.getLogger("core.access")


class IdentifyRequest(BaseModel):
    token: str


class SubscriptionRequest(BaseModel):
    user: dict[str, Any]
    subscription: dict[str, Any]


class LoginLinkMail(BaseModel):
    email: str
    # Contains the login token: never logged.
    url: str
    ttl_minutes: int


class UnsubscribedMail(BaseModel):
    email: str
    title: str
    manage_url: str


def create_app(core: Any) -> FastAPI:
    """`core` is a CoreApp (see app.py)."""
    app = FastAPI(title="Zabbix Digest Core", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def _access(request: Request, call_next):
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            if core.settings.log.access:
                # Core paths contain no secrets.
                client = request.client.host if request.client else "unknown"
                log_access(access_log, request.method, request.url.path, status, time.monotonic() - started, client)

    def require_key(x_internal_api_key: str | None = Header(default=None, alias=API_KEY_HEADER)) -> None:
        expected = core.settings.internal_api_key
        if not expected:
            raise HTTPException(status_code=404, detail="internal API is disabled")
        if not x_internal_api_key or not hmac.compare_digest(x_internal_api_key.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="invalid API key")

    internal = [Depends(require_key)]

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> Response:
        ok = core.ready()
        return JSONResponse({"status": "ready" if ok else "not ready"}, status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(core.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/internal/info", dependencies=internal)
    async def info() -> dict:
        current = core.source.current
        return {
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
            "timezone": core.tz.key,
            "revision": current.revision if current else None,
            "transports": core.transports.describe(),
        }

    @app.post("/internal/config/validate", dependencies=internal)
    async def validate(request: Request) -> Response:
        try:
            data = await request.json()
        except ValueError:
            return JSONResponse({"valid": False, "errors": ["request body is not JSON"]}, status_code=422)
        try:
            core.source.validate(data, "frontend")
        except UsersConfigError as exc:
            log.info("proposed configuration rejected (%d errors)", len(exc.errors), extra={"operation": "validate"})
            return JSONResponse({"valid": False, "errors": exc.errors}, status_code=422)
        return JSONResponse({"valid": True})

    @app.post("/internal/config/notify", dependencies=internal)
    async def notify() -> dict:
        changed = await core.source.refresh()
        current = core.source.current
        return {"changed": changed, "revision": current.revision if current else None}

    @app.post("/internal/zabbix/identify", dependencies=internal)
    async def identify(body: IdentifyRequest) -> Response:
        try:
            identity = await get_identity(core.zabbix, body.token)
        except ZabbixAuthError as exc:
            core.metrics.operations.labels(operation="identify", result="invalid").inc()
            log.info("token identification failed: token not accepted: %s", exc, extra={"operation": "identify"})
            return JSONResponse({"error": "invalid_token", "message": "The Zabbix API token is not valid."}, 422)
        except ZabbixTemporaryError as exc:
            core.metrics.operations.labels(operation="identify", result="unavailable").inc()
            log.warning("token identification failed: Zabbix unavailable: %s", exc,
                        extra={"operation": "identify", "error_category": "zabbix_unavailable"})
            return JSONResponse({"error": "zabbix_unavailable", "message": "Zabbix is temporarily unavailable.",
                                 "details": str(exc)}, 503)
        except ZabbixError as exc:
            core.metrics.operations.labels(operation="identify", result="error").inc()
            log.warning("token identification failed: Zabbix API error: %s", exc,
                        extra={"operation": "identify", "error_category": "zabbix_api"})
            return JSONResponse({"error": "zabbix_error", "message": str(exc)}, 502)
        core.metrics.operations.labels(operation="identify", result="ok").inc()
        return JSONResponse(
            {"userid": identity.userid, "username": identity.username, "active": identity.active, "emails": identity.emails}
        )

    def parse_subscription(body: SubscriptionRequest) -> tuple[User, Subscription]:
        try:
            user = User.model_validate({**body.user, "subscriptions": []})
            sub = Subscription.model_validate(body.subscription)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=format_validation_error(exc)) from None
        # All records of this request (including Zabbix and delivery) name the user and subscription.
        bind_log_context(user=user.email, subscription_id=sub.id)
        if sub.transport not in core.transports.names():
            raise HTTPException(status_code=422, detail=[f"subscription.transport: unknown transport '{sub.transport}'"])
        return user, sub

    def preparation_response(exc: PreparationError, operation: str, sub: Subscription) -> Response:
        status = 503 if exc.category == "zabbix_unavailable" else 422
        log.warning("%s failed: %s", operation.replace("_", " "), exc,
                    extra={"operation": operation, "error_category": exc.category})
        return JSONResponse(
            {"error": exc.category, "message": exc.user_message, "details": exc.details}, status_code=status
        )

    @app.post("/internal/preview", dependencies=internal)
    async def preview(body: SubscriptionRequest) -> Response:
        user, sub = parse_subscription(body)
        try:
            result = await core.processor.preview(user, sub)
        except PreviewNotSupported:
            return JSONResponse({"error": "preview_not_supported", "message": "Preview is not supported."}, 400)
        except PreparationError as exc:
            core.metrics.operations.labels(operation="preview", result="error").inc()
            return preparation_response(exc, "preview", sub)
        except RenderError as exc:
            core.metrics.operations.labels(operation="preview", result="error").inc()
            return JSONResponse({"error": "render", "message": "The report could not be rendered.", "details": str(exc)}, 500)
        core.metrics.operations.labels(operation="preview", result="ok").inc()
        return JSONResponse(result)

    @app.post("/internal/send-now", dependencies=internal)
    async def send_now(body: SubscriptionRequest) -> Response:
        user, sub = parse_subscription(body)
        try:
            result = await core.processor.send_now(user, sub)
        except PreparationError as exc:
            core.metrics.operations.labels(operation="send_now", result="error").inc()
            return preparation_response(exc, "send_now", sub)
        except (RenderError, DeliveryFailed) as exc:
            core.metrics.operations.labels(operation="send_now", result="error").inc()
            log.warning("send now failed: %s", exc, extra={"operation": "send_now"})
            return JSONResponse({"error": "delivery_failed", "message": "The digest could not be delivered."}, 502)
        except ShuttingDown:
            return JSONResponse({"error": "shutting_down", "message": "The service is shutting down."}, 503)
        core.metrics.operations.labels(operation="send_now", result=result["result"]).inc()
        return JSONResponse(result)

    @app.post("/internal/test-send", dependencies=internal)
    async def test_send(body: SubscriptionRequest) -> Response:
        user, sub = parse_subscription(body)
        try:
            await core.processor.test_send(user, sub)
        except (RenderError, DeliveryFailed) as exc:
            core.metrics.operations.labels(operation="test_send", result="error").inc()
            log.warning("test send failed: %s", exc, extra={"operation": "test_send"})
            return JSONResponse({"error": "delivery_failed", "message": "The test message could not be delivered."}, 502)
        except ShuttingDown:
            return JSONResponse({"error": "shutting_down", "message": "The service is shutting down."}, 503)
        core.metrics.operations.labels(operation="test_send", result="ok").inc()
        log.info("test message sent", extra={"operation": "test_send"})
        return JSONResponse({"sent": True})

    async def system_mail(operation: str, send) -> Response:
        try:
            await send
        except Exception as exc:  # noqa: BLE001
            core.metrics.operations.labels(operation=operation, result="error").inc()
            details = f"SMTP {smtp_target(core.settings.smtp)}: {describe_error(exc)}"
            log.warning("%s not sent: %s", operation, details, extra={"operation": operation})
            return JSONResponse(
                {"error": "delivery_failed", "message": "The email could not be delivered.", "details": details}, 502
            )
        core.metrics.operations.labels(operation=operation, result="ok").inc()
        log.info("%s sent via %s", operation, smtp_target(core.settings.smtp), extra={"operation": operation})
        return JSONResponse({"sent": True})

    @app.post("/internal/mail/login-link", dependencies=internal)
    async def login_link_mail(body: LoginLinkMail) -> Response:
        bind_log_context(user=body.email)
        return await system_mail("login_link", core.mailer.send_login_link(body.email, body.url, body.ttl_minutes))

    @app.post("/internal/mail/unsubscribed", dependencies=internal)
    async def unsubscribed_mail(body: UnsubscribedMail) -> Response:
        bind_log_context(user=body.email)
        return await system_mail("unsubscribed", core.mailer.send_unsubscribed(body.email, body.title, body.manage_url))

    return app
