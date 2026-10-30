"""Web backend routes."""

import asyncio
import contextlib
import hmac
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from common.access import log_access
from common.errors import describe_error
from common.ids import IdSpaceExhausted, new_subscription_id
from common.logs import REDACTOR, bind_log_context
from common.sentry import capture_exception
from common.version import __version__
from .auth import IMPERSONATION_COOKIE, SESSION_COOKIE, AuthContext, LoginLinks, Sessions, Signer, new_generation
from .config import WebSettings
from .core_client import API_KEY_HEADER, ConfigRejected, CoreClient, CoreOperationError, CoreUnavailable
from .metrics import WebMetrics
from .ratelimit import RateLimiter
from .store import (
    NO_CHANGE,
    RevisionConflict,
    StoreError,
    UsersStore,
    all_subscription_ids,
    find_subscription_by_token,
    find_user,
    public_user,
    subscription_title,
)

log = logging.getLogger(__name__)
access_log = logging.getLogger("web.access")
# UI paths that carry a token.
_TOKEN_PATH_RE = re.compile(r"^/(login|unsubscribe)/[^/]+")


def access_path(request: Request) -> str:
    """Request path for the access log, without tokens."""
    route = getattr(request.scope.get("route"), "path", None)
    if route and "{" in route and route != "/{path:path}":
        return route  # e.g. /api/auth/link/{token}
    return _TOKEN_PATH_RE.sub(r"/\1/{token}", request.url.path)
audit = logging.getLogger("zabbix_digest.audit")

CORE_UNAVAILABLE_MESSAGE = (
    "The digest service is temporarily unavailable, so the change was not saved. "
    "Try again later. If the problem persists, contact your administrator."
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# Request bodies.


class TokenLogin(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    email: str | None = Field(default=None, max_length=320)


class EmailLogin(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class LinkLogin(BaseModel):
    token: str = Field(min_length=1, max_length=1000)


class SubscriptionIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    widget_url: str = Field(min_length=1, max_length=2000)
    time: str = Field(min_length=4, max_length=5)
    transport: str = "email"
    enabled: bool = True
    # Filter overrides. Validated by Core together with the whole configuration.
    min_severity: int | None = None
    hide_acknowledged: bool = False
    hide_suppressed: bool = False


class TokenChange(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class EmailChange(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class DeleteProfile(BaseModel):
    confirm: str


class Impersonate(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class WebApp:
    def __init__(self, settings: WebSettings, core_transport=None) -> None:
        self.settings = settings
        self.metrics = WebMetrics()
        REDACTOR.add_static([settings.internal_api_key, settings.session_secret])
        self.core = CoreClient(settings.core_url, settings.internal_api_key, settings.core_timeout, self.metrics,
                               transport=core_transport, use_proxy=settings.proxy_use_internal)
        self.store = UsersStore(settings.users_file, self.core, self.metrics)
        signer = Signer(settings.session_secret)
        self.sessions = Sessions(signer, settings.session_max_age, settings.impersonation_ttl, settings.admin_emails)
        Path(settings.state_dir).mkdir(parents=True, exist_ok=True)
        self.links = LoginLinks(signer, settings.login_link_ttl, settings.state_dir)
        self.limiter = RateLimiter()
        self._background: set[asyncio.Task] = set()

    def background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def aclose(self) -> None:
        for task in list(self._background):
            try:
                await asyncio.wait_for(task, timeout=10)
            except Exception:  # noqa: BLE001
                pass
        await self.core.aclose()

    # Helpers.

    def client_ip(self, request: Request) -> str:
        if self.settings.trust_proxy_headers:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def limit(self, name: str, key: str) -> None:
        rule = getattr(self.settings.rate_limits, name)
        if not self.limiter.hit(f"{name}:{key}", rule.limit, rule.period):
            self.metrics.rate_limited.labels(limit=name).inc()
            raise ApiError(429, "rate_limited", "Too many requests. Try again later.")

    def set_session(self, response: Response, user: dict) -> None:
        response.set_cookie(
            SESSION_COOKIE, self.sessions.session_token(user), max_age=int(self.settings.session_max_age),
            httponly=True, secure=self.settings.cookie_secure, samesite="lax", path="/",
        )

    def clear_cookies(self, response: Response, session: bool = True) -> None:
        if session:
            response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(IMPERSONATION_COOKIE, path="/")

    def read_users(self) -> dict:
        try:
            return self.store.read()
        except StoreError as exc:
            raise ApiError(500, "storage", "The user configuration cannot be read.") from exc

    def context(self, request: Request) -> AuthContext | None:
        ctx = self.sessions.resolve(
            self.read_users(), request.cookies.get(SESSION_COOKIE), request.cookies.get(IMPERSONATION_COOKIE)
        )
        if ctx:
            # Records of this request name the user; during impersonation also the administrator.
            bind_log_context(user=ctx.user["email"], actor=ctx.real["email"] if ctx.impersonating else None)
        return ctx

    def audit(self, ctx: AuthContext | None, action: str, **fields: Any) -> None:
        actor = ctx.real["email"] if ctx else fields.pop("actor", "anonymous")
        extra = {"operation": action, "actor": actor}
        if ctx and ctx.impersonating:
            extra["target"] = ctx.user["email"]
        details = " ".join(f"{k}={v}" for k, v in fields.items())
        audit.info("audit: %s%s%s", action, " " if details else "", details, extra=extra)

    async def mutate(self, change):
        try:
            return await self.store.mutate(change)
        except ConfigRejected as exc:
            raise ApiError(422, "invalid", "The change was rejected: " + "; ".join(exc.errors[:5]), exc.errors) from None
        except CoreUnavailable:
            raise ApiError(503, "core_unavailable", CORE_UNAVAILABLE_MESSAGE) from None
        except RevisionConflict:
            raise ApiError(409, "conflict", "The configuration was changed by another operation. Press Save again.") from None
        except StoreError as exc:
            raise ApiError(500, "storage", f"The change could not be saved: {exc}") from None

    async def identify(self, token: str) -> dict:
        try:
            return await self.core.identify(token)
        except CoreOperationError as exc:
            if exc.code != "invalid_token":
                log.warning("token identification failed: core answered %s", exc.describe(),
                            extra={"operation": "identify"})
            if exc.code == "invalid_token":
                self.metrics.auth_failures.labels(reason="invalid_token").inc()
                raise ApiError(401, "invalid_token", "The Zabbix API token is not valid.") from None
            if exc.status == 503:
                raise ApiError(503, "zabbix_unavailable", "Zabbix is temporarily unavailable. Try again later.") from None
            raise ApiError(502, "zabbix_error", "Zabbix returned an error. Contact your administrator.") from None
        except CoreUnavailable:
            raise ApiError(503, "core_unavailable", "The digest service is temporarily unavailable. Try again later.") from None

    async def transports(self) -> list[dict]:
        try:
            return (await self.core.info())["transports"]
        except CoreUnavailable:
            raise ApiError(503, "core_unavailable", CORE_UNAVAILABLE_MESSAGE) from None


def mail_error(exc: Exception) -> str:
    """Log text for a failed system email (sent by Core)."""
    if isinstance(exc, CoreOperationError):
        return "core answered " + exc.describe()
    if isinstance(exc, CoreUnavailable):
        return "core is unavailable (see the previous message)"
    return describe_error(exc)


def own_subscription(user: dict, sub_id: str) -> dict:
    for sub in user.get("subscriptions") or []:
        if sub.get("id") == sub_id:
            bind_log_context(subscription_id=sub_id)
            return sub
    raise ApiError(404, "not_found", "Subscription not found.")


def subscription_out(sub: dict) -> dict:
    return {
        "id": sub["id"],
        "name": sub.get("name"),
        "title": subscription_title(sub),
        "widget_url": sub.get("widget_url"),
        "time": sub.get("time"),
        "transport": sub.get("transport", "email"),
        "enabled": sub.get("enabled", True),
        "min_severity": sub.get("min_severity"),
        "hide_acknowledged": sub.get("hide_acknowledged", False),
        "hide_suppressed": sub.get("hide_suppressed", False),
    }


def set_overrides(sub: dict, body: SubscriptionIn) -> None:
    # Keys with default values are omitted, to keep users.yaml short.
    for key, value, default in (
        ("min_severity", body.min_severity, None),
        ("hide_acknowledged", body.hide_acknowledged, False),
        ("hide_suppressed", body.hide_suppressed, False),
    ):
        if value == default:
            sub.pop(key, None)
        else:
            sub[key] = value


def create_app(settings: WebSettings, core_transport=None) -> FastAPI:
    web = WebApp(settings, core_transport=core_transport)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await web.aclose()

    app = FastAPI(
        title="Zabbix Digest", version=__version__, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.web = web

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        if exc.status >= 500:
            log.warning("%s %s: %d %s: %s", request.method, access_path(request), exc.status, exc.code, exc.message)
        body = {"error": exc.code, "message": exc.message}
        if exc.details is not None:
            body["details"] = exc.details
        return JSONResponse(body, status_code=exc.status)

    @app.middleware("http")
    async def _middleware(request: Request, call_next):
        started = time.monotonic()
        path = request.url.path
        if path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # Custom header: cross-site forms cannot send it without a CORS preflight.
            if request.headers.get("x-requested-with") != "zabbix-digest":
                return JSONResponse({"error": "csrf", "message": "Missing request header."}, status_code=403)
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            web.metrics.unexpected_errors.inc()
            log.exception("unexpected error", extra={"operation": path})
            capture_exception(exc)
            response = JSONResponse({"error": "internal", "message": "Internal error."}, status_code=500)
        if settings.log.access:
            log_access(access_log, request.method, access_path(request), response.status_code,
                       time.monotonic() - started, web.client_ip(request))
        route = request.scope.get("route")
        label = getattr(route, "path", "other")
        web.metrics.requests.labels(method=request.method, route=label, status=str(response.status_code)).inc()
        web.metrics.request_duration.labels(method=request.method, route=label).observe(time.monotonic() - started)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if path.startswith(("/api/", "/internal/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def ctx_required(request: Request) -> AuthContext:
        ctx = web.context(request)
        if ctx is None:
            raise ApiError(401, "unauthenticated", "Please log in.")
        return ctx

    def require_key(x_internal_api_key: str | None = Header(default=None, alias=API_KEY_HEADER)) -> None:
        if not x_internal_api_key or not hmac.compare_digest(
            x_internal_api_key.encode(), settings.internal_api_key.encode()
        ):
            raise HTTPException(status_code=401, detail="invalid API key")

    # Health, metrics, internal.

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> Response:
        ok = web.store.accessible()
        return JSONResponse({"status": "ready" if ok else "not ready"}, status_code=200 if ok else 503)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(web.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/internal/config", dependencies=[Depends(require_key)])
    async def internal_config() -> dict:
        return web.read_users()

    # Public.

    @app.get("/api/public-config")
    async def public_config() -> dict:
        return {
            "version": __version__,
            "sentry_dsn": settings.sentry_browser_dsn,
            "sentry_environment": settings.sentry_environment,
            "help_url": settings.help_url,
        }

    # Authentication.

    @app.post("/api/auth/token")
    async def token_login(body: TokenLogin, request: Request) -> Response:
        web.limit("token_auth_per_ip", web.client_ip(request))
        identity = await web.identify(body.token.strip())
        if not identity.get("active", True):
            web.metrics.auth_failures.labels(reason="user_disabled").inc()
            raise ApiError(403, "user_disabled", "Your Zabbix user is disabled.")
        emails = identity.get("emails") or []
        if not emails:
            raise ApiError(403, "no_email", "No enabled email media is configured for your Zabbix user.")
        data = web.read_users()
        if body.email is None:
            return JSONResponse({
                "step": "choose_email",
                "emails": [{"email": e, "registered": find_user(data, email=e) is not None} for e in emails],
            })
        email = next((e for e in emails if e.lower() == body.email.strip().lower()), None)
        if email is None:
            raise ApiError(400, "email_not_owned", "This email address is not configured for your Zabbix user.")
        bind_log_context(user=email)
        token = body.token.strip()
        userid = str(identity["userid"])
        existing = find_user(data, email=email)
        if existing is not None:
            await check_same_zabbix_user(existing, userid)

        def change(cfg: dict):
            user = find_user(cfg, email=email)
            if user is None:
                if existing is not None:
                    raise ApiError(409, "conflict", "The profile was changed meanwhile. Try again.")
                user = {
                    "email": email, "zabbix_token": token, "zabbix_userid": userid,
                    "generation": new_generation(), "session_version": 1, "subscriptions": [],
                }
                cfg["users"].append(user)
                return user
            if user.get("zabbix_userid") and user["zabbix_userid"] != userid:
                raise ApiError(403, "other_user", "This email address is registered for a different Zabbix user.")
            changed = False
            for key, value in (("zabbix_token", token), ("zabbix_userid", userid)):
                if user.get(key) != value:
                    user[key] = value
                    changed = True
            if not user.get("generation"):
                user["generation"] = new_generation()
                changed = True
            return user if changed else NO_CHANGE

        result = await web.mutate(change)
        user = find_user(web.read_users(), email=email) if result is NO_CHANGE else result
        response = JSONResponse({"ok": True, "registered": existing is None})
        web.set_session(response, user)
        web.audit(None, "register" if existing is None else "login_token", actor=email)
        return response

    async def check_same_zabbix_user(profile: dict, userid: str) -> None:
        """A new token must belong to the Zabbix user of the existing profile."""
        if profile.get("zabbix_userid"):
            if profile["zabbix_userid"] != userid:
                web.metrics.auth_failures.labels(reason="other_user").inc()
                raise ApiError(403, "other_user", "This email address is registered for a different Zabbix user.")
            return
        try:
            stored = await web.core.identify(profile["zabbix_token"])
        except CoreOperationError as exc:
            if exc.code == "invalid_token":
                return  # stored token is invalid; the email ownership check is the proof
            log.warning("stored token check failed: core answered %s", exc.describe(), extra={"operation": "identify"})
            raise ApiError(503, "zabbix_unavailable", "Zabbix is temporarily unavailable. Try again later.") from None
        except CoreUnavailable:
            raise ApiError(503, "core_unavailable", CORE_UNAVAILABLE_MESSAGE) from None
        if str(stored.get("userid")) != userid:
            web.metrics.auth_failures.labels(reason="other_user").inc()
            raise ApiError(403, "other_user", "This email address is registered for a different Zabbix user.")

    @app.post("/api/auth/email")
    async def email_login(body: EmailLogin, request: Request) -> dict:
        email = body.email.strip().lower()
        bind_log_context(user=email)
        web.limit("login_link_per_ip", web.client_ip(request))
        web.limit("login_link_per_email", email)
        user = find_user(web.read_users(), email=email)
        if user is None:
            web.metrics.login_links.labels(result="unknown").inc()
        else:
            web.background(send_login_link(user["email"]))
        # Same answer whether or not the address is registered.
        return {"message": "If this address is registered, a login link has been sent to it."}

    async def send_login_link(email: str) -> None:
        try:
            user = find_user(web.read_users(), email=email)
            if user is None:
                return
            if not user.get("generation"):

                def change(cfg: dict):
                    target = find_user(cfg, email=email)
                    if target is None or target.get("generation"):
                        return NO_CHANGE
                    target["generation"] = new_generation()
                    return target

                await web.store.mutate(change)
                user = find_user(web.read_users(), email=email)
            token = web.links.create(user["generation"])
            url = f"{settings.public_url}/login/{token}"
            await web.core.send_login_link(user["email"], url, max(1, int(settings.login_link_ttl // 60)))
            web.metrics.login_links.labels(result="sent").inc()
            web.audit(None, "login_link_sent", actor=user["email"])
        except Exception as exc:  # noqa: BLE001
            web.metrics.login_links.labels(result="failed").inc()
            log.warning("login link not sent: %s", mail_error(exc), extra={"operation": "login_link"})

    @app.post("/api/auth/link")
    async def link_login(body: LinkLogin, request: Request) -> Response:
        web.limit("link_consume_per_ip", web.client_ip(request))
        invalid = ApiError(400, "invalid_link", "This login link is invalid, expired or already used.")
        data = web.links.parse(body.token)
        if data is None or await web.links.is_used(data["j"]):
            web.metrics.auth_failures.labels(reason="invalid_link").inc()
            raise invalid
        user = find_user(web.read_users(), generation=data["g"])
        if user is None:
            web.metrics.auth_failures.labels(reason="invalid_link").inc()
            raise invalid
        bind_log_context(user=user["email"])
        try:
            identity = await web.core.identify(user["zabbix_token"])
        except CoreOperationError as exc:
            if exc.code == "invalid_token":
                web.metrics.auth_failures.labels(reason="stored_token_invalid").inc()
                raise ApiError(
                    409, "token_required",
                    "Your saved Zabbix API token is no longer valid. Log in with a new API token.",
                ) from None
            log.warning("login link check failed: core answered %s", exc.describe(), extra={"operation": "identify"})
            raise ApiError(503, "zabbix_unavailable", "Zabbix is temporarily unavailable. Try again later.") from None
        except CoreUnavailable:
            raise ApiError(503, "core_unavailable", "The digest service is temporarily unavailable. Try again later.") from None
        if not identity.get("active", True):
            raise ApiError(403, "user_disabled", "Your Zabbix user is disabled.")
        if user.get("zabbix_userid") and str(identity.get("userid")) != user["zabbix_userid"]:
            raise ApiError(409, "token_required", "Your saved Zabbix API token belongs to another user. Log in with a new API token.")
        if user["email"].lower() not in {e.lower() for e in identity.get("emails") or []}:
            raise ApiError(403, "email_not_owned", "Your email address is no longer configured for your Zabbix user.")
        if not await web.links.consume(data["j"]):
            raise invalid
        response = JSONResponse({"ok": True})
        web.set_session(response, user)
        web.audit(None, "login_link", actor=user["email"])
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> Response:
        ctx = web.context(request)
        response = JSONResponse({"ok": True})
        web.clear_cookies(response)
        if ctx:
            web.audit(ctx, "logout")
        return response

    @app.post("/api/auth/logout-all")
    async def logout_all(request: Request) -> Response:
        ctx = ctx_required(request)
        generation = ctx.user["generation"]

        def change(cfg: dict):
            user = find_user(cfg, generation=generation)
            if user is None:
                raise ApiError(404, "not_found", "Profile not found.")
            user["session_version"] = int(user.get("session_version", 1)) + 1
            return user

        await web.mutate(change)
        web.audit(ctx, "logout_all")
        response = JSONResponse({"ok": True})
        web.clear_cookies(response, session=not ctx.impersonating)
        return response

    # Profile.

    @app.get("/api/me")
    async def me(request: Request) -> dict:
        ctx = ctx_required(request)
        user = ctx.user
        try:
            info = await web.core.info()
        except CoreUnavailable:
            info = {"timezone": None, "transports": []}
        return {
            "email": user["email"],
            "token_configured": bool(user.get("zabbix_token")),
            "is_admin": ctx.admin,
            "impersonating": ctx.impersonating,
            "real_email": ctx.real["email"] if ctx.impersonating else None,
            "timezone": info.get("timezone"),
            "transports": [
                {"name": t["name"], "preview": t["preview"]} for t in info.get("transports", []) if t.get("public")
            ],
            "max_subscriptions": settings.max_subscriptions_per_user or None,
            "subscriptions": [subscription_out(s) for s in user.get("subscriptions") or []],
        }

    @app.post("/api/profile/token")
    async def change_token(body: TokenChange, request: Request) -> dict:
        ctx = ctx_required(request)
        token = body.token.strip()
        identity = await web.identify(token)
        user = ctx.user
        userid = str(identity["userid"])
        await check_same_zabbix_user(user, userid)
        if not identity.get("active", True):
            raise ApiError(403, "user_disabled", "This Zabbix user is disabled.")
        if user["email"].lower() not in {e.lower() for e in identity.get("emails") or []}:
            raise ApiError(400, "email_not_owned", "Your profile email is not configured for the Zabbix user of this token.")
        generation = user["generation"]

        def change(cfg: dict):
            target = find_user(cfg, generation=generation)
            if target is None:
                raise ApiError(404, "not_found", "Profile not found.")
            target["zabbix_token"] = token
            target["zabbix_userid"] = userid
            return target

        await web.mutate(change)
        web.audit(ctx, "token_replaced")
        return {"ok": True}

    @app.get("/api/profile/emails")
    async def profile_emails(request: Request) -> dict:
        ctx = ctx_required(request)
        identity = await web.identify(ctx.user["zabbix_token"])
        data = web.read_users()
        result = []
        for email in identity.get("emails") or []:
            other = find_user(data, email=email)
            result.append({
                "email": email,
                "current": email.lower() == ctx.user["email"].lower(),
                "available": other is None or other.get("generation") == ctx.user["generation"],
            })
        return {"emails": result}

    @app.post("/api/profile/email")
    async def change_email(body: EmailChange, request: Request) -> dict:
        ctx = ctx_required(request)
        identity = await web.identify(ctx.user["zabbix_token"])
        email = next((e for e in identity.get("emails") or [] if e.lower() == body.email.strip().lower()), None)
        if email is None:
            raise ApiError(400, "email_not_owned", "This email address is not configured for your Zabbix user.")
        generation = ctx.user["generation"]
        old_email = ctx.user["email"]

        def change(cfg: dict):
            other = find_user(cfg, email=email)
            if other is not None and other.get("generation") != generation:
                raise ApiError(409, "email_registered", "This email address is already registered in the service.")
            target = find_user(cfg, generation=generation)
            if target is None:
                raise ApiError(404, "not_found", "Profile not found.")
            if target["email"] == email:
                return NO_CHANGE
            target["email"] = email
            return target

        await web.mutate(change)
        web.audit(ctx, "email_changed", old=old_email, new=email)
        return {"ok": True, "email": email}

    @app.post("/api/profile/delete")
    async def delete_profile(body: DeleteProfile, request: Request) -> Response:
        ctx = ctx_required(request)
        if body.confirm != "DELETE":
            raise ApiError(400, "confirm", "Type DELETE to confirm.")
        generation = ctx.user["generation"]

        def change(cfg: dict):
            before = len(cfg["users"])
            cfg["users"] = [u for u in cfg["users"] if u.get("generation") != generation]
            if len(cfg["users"]) == before:
                raise ApiError(404, "not_found", "Profile not found.")
            return True

        await web.mutate(change)
        web.audit(ctx, "profile_deleted", profile=ctx.user["email"])
        response = JSONResponse({"ok": True})
        web.clear_cookies(response, session=not ctx.impersonating)
        return response

    # Subscriptions.

    def check_transport(name: str, transports: list[dict], previous: str | None = None) -> None:
        if name == previous:
            return
        if not any(t["name"] == name and t.get("public") for t in transports):
            raise ApiError(400, "transport", "This delivery method is not available.")

    @app.post("/api/subscriptions")
    async def create_subscription(body: SubscriptionIn, request: Request) -> dict:
        ctx = ctx_required(request)
        check_transport(body.transport, await web.transports())
        generation = ctx.user["generation"]

        def change(cfg: dict):
            user = find_user(cfg, generation=generation)
            if user is None:
                raise ApiError(404, "not_found", "Profile not found.")
            subs = user.setdefault("subscriptions", [])
            limit = settings.max_subscriptions_per_user
            if limit and len(subs) >= limit:
                raise ApiError(400, "limit", f"You can have at most {limit} subscriptions.")
            try:
                sub_id = new_subscription_id(all_subscription_ids(cfg), settings.subscription_id_length)
            except IdSpaceExhausted:
                raise ApiError(503, "id_space", "No free subscription IDs left. Contact your administrator.") from None
            sub = {
                "id": sub_id,
                "name": (body.name or "").strip() or None,
                "widget_url": body.widget_url.strip(),
                "time": body.time,
                "transport": body.transport,
                "enabled": body.enabled,
                "unsubscribe_token": secrets.token_urlsafe(24),
            }
            set_overrides(sub, body)
            subs.append(sub)
            return sub

        sub = await web.mutate(change)
        web.audit(ctx, "subscription_created", subscription=sub["id"])
        return subscription_out(sub)

    @app.put("/api/subscriptions/{sub_id}")
    async def update_subscription(sub_id: str, body: SubscriptionIn, request: Request) -> dict:
        ctx = ctx_required(request)
        previous = own_subscription(ctx.user, sub_id)
        check_transport(body.transport, await web.transports(), previous.get("transport", "email"))
        generation = ctx.user["generation"]

        def change(cfg: dict):
            user = find_user(cfg, generation=generation)
            if user is None:
                raise ApiError(404, "not_found", "Profile not found.")
            sub = own_subscription(user, sub_id)
            sub.update(
                name=(body.name or "").strip() or None,
                widget_url=body.widget_url.strip(),
                time=body.time,
                transport=body.transport,
                enabled=body.enabled,
            )
            set_overrides(sub, body)
            if not sub.get("unsubscribe_token"):
                sub["unsubscribe_token"] = secrets.token_urlsafe(24)
            return sub

        sub = await web.mutate(change)
        web.audit(ctx, "subscription_updated", subscription=sub_id, enabled=sub["enabled"])
        return subscription_out(sub)

    @app.delete("/api/subscriptions/{sub_id}")
    async def delete_subscription(sub_id: str, request: Request) -> dict:
        ctx = ctx_required(request)
        own_subscription(ctx.user, sub_id)
        generation = ctx.user["generation"]

        def change(cfg: dict):
            user = find_user(cfg, generation=generation)
            if user is None:
                raise ApiError(404, "not_found", "Profile not found.")
            own_subscription(user, sub_id)
            user["subscriptions"] = [s for s in user["subscriptions"] if s.get("id") != sub_id]
            return True

        await web.mutate(change)
        web.audit(ctx, "subscription_deleted", subscription=sub_id)
        return {"ok": True}

    def operation_error(exc: CoreOperationError, operation: str) -> ApiError:
        log.warning("%s failed: core answered %s", operation.replace("_", " "), exc.describe(),
                    extra={"operation": operation})
        body_status = {422: 400, 400: 400, 503: 503}.get(exc.status, 502)
        message = exc.message + (f" ({exc.details})" if exc.details else "")
        return ApiError(body_status, exc.code, message)

    @app.post("/api/subscriptions/{sub_id}/preview")
    async def preview(sub_id: str, request: Request) -> dict:
        ctx = ctx_required(request)
        sub = own_subscription(ctx.user, sub_id)
        try:
            result = await web.core.preview(public_user(ctx.user), sub)
        except CoreOperationError as exc:
            raise operation_error(exc, "preview") from None
        except CoreUnavailable:
            raise ApiError(503, "core_unavailable", "The digest service is temporarily unavailable.") from None
        web.audit(ctx, "preview", subscription=sub_id)
        return result

    @app.post("/api/subscriptions/{sub_id}/send-now")
    async def send_now(sub_id: str, request: Request) -> dict:
        ctx = ctx_required(request)
        sub = own_subscription(ctx.user, sub_id)
        try:
            result = await web.core.send_now(public_user(ctx.user), sub)
        except CoreOperationError as exc:
            raise operation_error(exc, "send_now") from None
        except CoreUnavailable:
            raise ApiError(503, "core_unavailable", "The digest service is temporarily unavailable.") from None
        web.audit(ctx, "send_now", subscription=sub_id, result=result.get("result"))
        return {
            "result": result.get("result"),
            "shown": result.get("shown"),
            "total": result.get("total"),
            "total_exact": result.get("total_exact"),
        }

    @app.post("/api/subscriptions/{sub_id}/test-send")
    async def test_send(sub_id: str, request: Request) -> dict:
        ctx = ctx_required(request)
        sub = own_subscription(ctx.user, sub_id)
        try:
            await web.core.test_send(public_user(ctx.user), sub)
        except CoreOperationError as exc:
            raise operation_error(exc, "test_send") from None
        except CoreUnavailable:
            raise ApiError(503, "core_unavailable", "The digest service is temporarily unavailable.") from None
        web.audit(ctx, "test_send", subscription=sub_id)
        return {"ok": True}

    # Impersonation.

    @app.post("/api/admin/impersonate")
    async def impersonate(body: Impersonate, request: Request) -> Response:
        ctx = ctx_required(request)
        if not web.sessions.is_admin(ctx.real):
            web.metrics.auth_failures.labels(reason="not_admin").inc()
            raise ApiError(403, "forbidden", "Not allowed.")
        email = body.email.strip()
        target = find_user(web.read_users(), email=email)
        if target is None:
            raise ApiError(404, "not_found", "No profile with this email address.")
        if not target.get("generation"):

            def change(cfg: dict):
                t = find_user(cfg, email=email)
                if t is None or t.get("generation"):
                    return NO_CHANGE
                t["generation"] = new_generation()
                return t

            await web.mutate(change)
            target = find_user(web.read_users(), email=email)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            IMPERSONATION_COOKIE, web.sessions.impersonation_token(ctx.real, target),
            httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
        )
        web.audit(ctx, "impersonation_started", target_email=target["email"])
        return response

    @app.post("/api/admin/stop-impersonation")
    async def stop_impersonation(request: Request) -> Response:
        ctx = web.context(request)
        response = JSONResponse({"ok": True})
        response.delete_cookie(IMPERSONATION_COOKIE, path="/")
        if ctx and ctx.impersonating:
            web.audit(ctx, "impersonation_stopped")
        return response

    # Unsubscribe (no authentication; the token is the credential).

    @app.get("/api/unsubscribe/{token}")
    async def unsubscribe_info(token: str, request: Request) -> dict:
        web.limit("unsubscribe_per_ip", web.client_ip(request))
        found = find_subscription_by_token(web.read_users(), token)
        if found is None:
            raise ApiError(404, "not_found", "This unsubscribe link is not valid. The subscription may have been deleted.")
        user, sub = found
        bind_log_context(user=user["email"], subscription_id=sub["id"])
        return {"title": subscription_title(sub), "enabled": sub.get("enabled", True)}

    @app.post("/api/unsubscribe/{token}")
    async def unsubscribe(token: str, request: Request) -> dict:
        web.limit("unsubscribe_per_ip", web.client_ip(request))
        found = find_subscription_by_token(web.read_users(), token)
        if found is not None:
            bind_log_context(user=found[0]["email"], subscription_id=found[1]["id"])

        def change(cfg: dict):
            found = find_subscription_by_token(cfg, token)
            if found is None:
                raise ApiError(404, "not_found", "This unsubscribe link is not valid. The subscription may have been deleted.")
            user, sub = found
            if not sub.get("enabled", True):
                return NO_CHANGE
            sub["enabled"] = False
            return user["email"], subscription_title(sub), sub["id"]

        result = await web.mutate(change)
        if result is NO_CHANGE:
            return {"status": "already_disabled"}
        email, title, sub_id = result
        web.audit(None, "unsubscribed", actor=email, subscription=sub_id)
        web.background(send_unsubscribed(email, title))
        return {"status": "disabled"}

    async def send_unsubscribed(email: str, title: str) -> None:
        try:
            await web.core.send_unsubscribed(email, title, settings.public_url + "/")
        except Exception as exc:  # noqa: BLE001
            log.warning("unsubscribe confirmation not sent: %s", mail_error(exc), extra={"operation": "unsubscribed"})

    # Vue application.

    static = Path(settings.static_dir) if settings.static_dir else None
    if static and (static / "index.html").is_file():
        if (static / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")
        index = static / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> Response:
            if path.startswith(("api/", "internal/", "health/")):
                raise HTTPException(status_code=404)
            candidate = (static / path).resolve()
            if path and candidate.is_file() and static.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
    else:
        log.warning("static_dir has no index.html; the web UI is not served")

    return app
