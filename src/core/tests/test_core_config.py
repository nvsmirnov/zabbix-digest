import pytest

from common.durations import parse_duration
from common.ids import IdSpaceExhausted, new_subscription_id
from common.settings import SettingsError
from common.yamlio import DuplicateKeyError, loads_yaml
from core.config import load_core_settings


def test_duration_parsing():
    assert parse_duration("30s") == 30
    assert parse_duration("2h") == 7200
    assert parse_duration("1h30m") == 5400
    assert parse_duration(5) == 5
    assert parse_duration("10") == 10
    for bad in ("", "abc", "5x", "1h 2", True):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_yaml_and_env_precedence(tmp_path, monkeypatch):
    path = tmp_path / "core.yaml"
    path.write_text("smtp:\n  host: yaml-host\n  port: 2525\nscheduler:\n  max_workers: 3\n")
    monkeypatch.setenv("SMTP__HOST", "env-host")
    monkeypatch.setenv("SCHEDULER__DELIVERY_WINDOW", "1h")
    settings = load_core_settings(str(path))
    assert settings.smtp.host == "env-host"
    assert settings.smtp.port == 2525
    assert settings.scheduler.max_workers == 3
    assert settings.scheduler.delivery_window == 3600
    assert settings.scheduler.tick_interval == 30


def test_env_only(monkeypatch):
    monkeypatch.setenv("ZABBIX__URL", "https://z.example.com/")
    monkeypatch.setenv("TRANSPORTS__EMAIL__PUBLIC", "false")
    monkeypatch.setenv("TRANSPORTS__EMAIL__TYPE", "email")
    settings = load_core_settings(None)
    assert settings.zabbix.url == "https://z.example.com"
    assert settings.transports["email"].public is False


def test_unknown_key_rejected(tmp_path):
    path = tmp_path / "core.yaml"
    path.write_text("smtp:\n  hostname: x\n")
    with pytest.raises(SettingsError) as info:
        load_core_settings(str(path))
    assert "smtp.hostname" in str(info.value)


def test_secret_not_in_error(tmp_path):
    path = tmp_path / "core.yaml"
    path.write_text("internal_api_key: short-secret\n")
    with pytest.raises(SettingsError) as info:
        load_core_settings(str(path))
    assert "short-secret" not in str(info.value)


def test_http_mode_requires_frontend(tmp_path):
    path = tmp_path / "core.yaml"
    path.write_text("users:\n  source: http\n")
    with pytest.raises(SettingsError):
        load_core_settings(str(path))


def test_delivery_window_limit(tmp_path):
    path = tmp_path / "core.yaml"
    path.write_text("scheduler:\n  delivery_window: 13h\n")
    with pytest.raises(SettingsError):
        load_core_settings(str(path))


def test_duplicate_yaml_keys():
    with pytest.raises(DuplicateKeyError):
        loads_yaml("a: 1\na: 2\n")


def test_subscription_id_generation():
    taken = {"abcd", "0", "12345678"}
    new = new_subscription_id(taken, 4)
    assert len(new) == 4 and new not in taken
    assert all(c in "0123456789abcdef" for c in new)
    one = {f"{i:x}" for i in range(15)}
    assert new_subscription_id(one, 1) == "f"
    with pytest.raises(IdSpaceExhausted):
        new_subscription_id({f"{i:x}" for i in range(16)}, 1)
