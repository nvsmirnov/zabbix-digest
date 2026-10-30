"""Signed cookie sessions, impersonation sessions and one-time login links.

Session cookie: {"g": profile generation, "v": session_version}. Valid while both match the profile.
Impersonation cookie (browser-session cookie): admin g/v + target g/v + server-side expiry.
Login link: signed {"g", "j"} with max age; "j" is recorded as used in a small YAML file until expiry.
"""

import asyncio
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from common.yamlio import atomic_write_yaml, load_yaml
from .store import find_user

SESSION_COOKIE = "zd_session"
IMPERSONATION_COOKIE = "zd_impersonation"


def new_generation() -> str:
    return secrets.token_hex(16)


@dataclass
class AuthContext:
    real: dict
    user: dict
    admin: bool
    impersonating: bool


class Signer:
    def __init__(self, secret: str) -> None:
        self._s = URLSafeTimedSerializer(secret)

    def dump(self, data: dict, salt: str) -> str:
        return self._s.dumps(data, salt=salt)

    def load(self, token: str | None, salt: str, max_age: float) -> dict | None:
        if not token:
            return None
        try:
            data = self._s.loads(token, salt=salt, max_age=int(max_age))
        except (BadSignature, SignatureExpired):
            return None
        return data if isinstance(data, dict) else None


class Sessions:
    def __init__(self, signer: Signer, session_max_age: float, impersonation_ttl: float, admin_emails: list[str]) -> None:
        self.signer = signer
        self.max_age = session_max_age
        self.imp_ttl = impersonation_ttl
        self.admins = set(admin_emails)

    def session_token(self, user: dict) -> str:
        return self.signer.dump({"g": user["generation"], "v": int(user.get("session_version", 1))}, "session")

    def impersonation_token(self, admin: dict, target: dict) -> str:
        return self.signer.dump(
            {
                "ag": admin["generation"], "av": int(admin.get("session_version", 1)),
                "g": target["generation"], "v": int(target.get("session_version", 1)),
                "exp": int(time.time() + self.imp_ttl),
            },
            "impersonation",
        )

    @staticmethod
    def _match(data: dict | None, users: dict, g: str = "g", v: str = "v") -> dict | None:
        if not data or not isinstance(data.get(g), str):
            return None
        user = find_user(users, generation=data[g])
        if user is None or int(user.get("session_version", 1)) != data.get(v):
            return None
        return user

    def is_admin(self, user: dict) -> bool:
        return str(user.get("email", "")).lower() in self.admins

    def resolve(self, users: dict, session: str | None, impersonation: str | None) -> AuthContext | None:
        real = self._match(self.signer.load(session, "session", self.max_age), users)
        if real is None:
            return None
        admin = self.is_admin(real)
        imp = self.signer.load(impersonation, "impersonation", self.imp_ttl)
        if imp and admin and imp.get("exp", 0) > time.time():
            # The originating admin session must match the one that started impersonation.
            if imp.get("ag") == real["generation"] and imp.get("av") == int(real.get("session_version", 1)):
                target = self._match(imp, users)
                if target is not None:
                    return AuthContext(real=real, user=target, admin=True, impersonating=True)
        return AuthContext(real=real, user=real, admin=admin, impersonating=False)


class LoginLinks:
    def __init__(self, signer: Signer, ttl: float, state_dir: str) -> None:
        self.signer = signer
        self.ttl = ttl
        self.path = Path(state_dir) / "used_login_links.yaml"
        self._lock = asyncio.Lock()

    def create(self, generation: str) -> str:
        return self.signer.dump({"g": generation, "j": secrets.token_urlsafe(12)}, "login")

    def parse(self, token: str) -> dict | None:
        data = self.signer.load(token, "login", self.ttl)
        if not data or not isinstance(data.get("g"), str) or not isinstance(data.get("j"), str):
            return None
        return data

    def _load_used(self) -> dict[str, float]:
        if not self.path.exists():
            return {}
        data = load_yaml(self.path) or {}
        return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}

    async def is_used(self, jti: str) -> bool:
        async with self._lock:
            return jti in self._load_used()

    async def consume(self, jti: str) -> bool:
        """Mark the link as used. Returns False if it was already used."""
        async with self._lock:
            now = time.time()
            used = {k: v for k, v in self._load_used().items() if v > now}
            if jti in used:
                return False
            used[jti] = now + self.ttl + 60
            atomic_write_yaml(self.path, used, mode=0o600)
            return True
