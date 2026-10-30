import pytest

from core.users import UsersConfigError, validate_users_config

from fake_zabbix import TOKEN, WIDGET_URL


def base(**sub):
    s = {"id": "a1b2", "widget_url": WIDGET_URL, "time": "08:30"}
    s.update(sub)
    return {"revision": 3, "users": [{"email": "a@example.com", "zabbix_token": TOKEN, "subscriptions": [s]}]}


def test_valid_minimal():
    config = validate_users_config(base(), ["email"])
    sub = config.users[0].subscriptions[0]
    assert sub.transport == "email" and sub.enabled and sub.name is None
    assert sub.title == "#a1b2"


def test_yaml_sexagesimal_time():
    config = validate_users_config(base(time=510), ["email"])
    assert config.users[0].subscriptions[0].time == "08:30"
    config = validate_users_config(base(time="8:05"), ["email"])
    assert config.users[0].subscriptions[0].time == "08:05"


@pytest.mark.parametrize("value, expected", [(None, None), (0, 0), (4, 4), ("5", 5), ("High", 4), ("not classified", 0)])
def test_min_severity(value, expected):
    config = validate_users_config(base(min_severity=value), ["email"])
    assert config.users[0].subscriptions[0].min_severity == expected


@pytest.mark.parametrize(
    "sub, fragment",
    [
        ({"id": "XYZ"}, "id"),
        ({"time": "25:00"}, "time"),
        ({"transport": "telegram"}, "unknown transport"),
        ({"widget_url": "ftp://x/zabbix.php?dashboardid=1"}, "widget_url"),
        ({"widget_url": "https://z/zabbix.php?action=dashboard.view"}, "dashboardid"),
        ({"widget_url": "https://z/zabbix.php?action=problem.view&dashboardid=1"}, "widget_url"),
        ({"extra": 1}, "extra"),
        ({"min_severity": 6}, "min_severity"),
        ({"min_severity": -1}, "min_severity"),
        ({"min_severity": "critical"}, "min_severity"),
        ({"min_severity": True}, "min_severity"),
        ({"hide_acknowledged": "maybe"}, "hide_acknowledged"),
    ],
)
def test_invalid(sub, fragment):
    with pytest.raises(UsersConfigError) as info:
        validate_users_config(base(**sub), ["email"])
    assert fragment in str(info.value)


def test_duplicates():
    data = base()
    data["users"].append(
        {"email": "A@example.com", "zabbix_token": TOKEN, "subscriptions": [{"id": "a1b2", "widget_url": WIDGET_URL, "time": "09:00"}]}
    )
    with pytest.raises(UsersConfigError) as info:
        validate_users_config(data, ["email"])
    text = str(info.value)
    assert "duplicate email" in text and "duplicate subscription id" in text


def test_errors_do_not_contain_tokens():
    data = base()
    data["users"][0]["zabbix_token"] = 12345
    data["users"][0]["unknown"] = TOKEN
    with pytest.raises(UsersConfigError) as info:
        validate_users_config(data, ["email"])
    assert TOKEN not in str(info.value)
