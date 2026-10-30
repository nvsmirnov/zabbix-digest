from typing import Any

from core.config import CoreSettings

from fake_zabbix import TOKEN, WIDGET_URL


def make_settings(tmp_path, **overrides: Any) -> CoreSettings:
    data: dict[str, Any] = {
        "zabbix": {"url": "https://zabbix.example.com", "retry": {"attempts": 2, "initial_delay": 0, "max_delay": 0}},
        "users": {"source": "file", "file": str(tmp_path / "users.yaml"), "cache_file": str(tmp_path / "cache.yaml")},
        "state_dir": str(tmp_path / "state"),
        "scheduler": {"timezone": "UTC", "delivery_window": "2h", "retry_interval": "10m"},
        "transports": {
            "email": {
                "type": "email",
                "public": True,
                "send_timeout": "5s",
                "retry": {"attempts": 2, "initial_delay": 0, "max_delay": 0},
            }
        },
        "smtp": {"host": "127.0.0.1", "port": 2525, "from_address": "digest@example.com"},
        "http": {"enabled": False},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return CoreSettings(**data)


def users_data(**sub_overrides: Any) -> dict:
    sub = {"id": "a1b2", "name": "DB", "widget_url": WIDGET_URL, "time": "08:00", "transport": "email"}
    sub.update(sub_overrides)
    return {
        "revision": 1,
        "users": [{"email": "alice@example.com", "zabbix_token": TOKEN, "subscriptions": [sub]}],
    }


class RecordingAdapter:
    supports_preview = True

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail: list[Exception] = []

    async def send(self, message: dict) -> None:
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append(message)

    def render_preview(self, message: dict) -> dict:
        return {"content_type": "text/html", "subject": "s", "body": "<html></html>"}
