import pytest

from core.errors import (
    Category,
    PreparationError,
    ZabbixApiError,
    ZabbixAuthError,
    ZabbixTemporaryError,
)
from core.zabbix.client import ZabbixClient, classify_rpc_error
from core.zabbix.identity import extract_emails, validate_user
from core.zabbix.problems import fetch_problems
from core.zabbix.urls import WidgetUrlError, parse_widget_url
from core.zabbix.widget import (
    SORT_HOST_ASC,
    SORT_NAME_DESC,
    SORT_SEVERITY_DESC,
    SORT_TIME_ASC,
    WidgetSettings,
    ACK_ACK,
    ACK_ALL,
    ACK_UNACK,
    Overrides,
    apply_overrides,
    build_problem_params,
    build_settings,
    display_tags,
    expand_subgroups,
    find_problems_widget,
    parse_fields,
    sort_problems,
)
from common.retry import RetryConfig

from fake_zabbix import OTHER_TOKEN, TOKEN, FakeZabbix


def test_parse_widget_url():
    assert parse_widget_url("https://z/zabbix.php?action=dashboard.view&dashboardid=12&page=2").dashboardid == "12"
    assert parse_widget_url("https://z/zabbix/zabbix.php?dashboardid=7").dashboardid == "7"
    for bad in ("", "https://z/zabbix.php?action=dashboard.view", "https://z/hosts.php?dashboardid=1",
                "https://z/zabbix.php?dashboardid=1&dashboardid=2", "https://z/zabbix.php?dashboardid=x"):
        with pytest.raises(WidgetUrlError):
            parse_widget_url(bad)


def test_parse_fields_indexed_and_legacy():
    fields = parse_fields([
        {"name": "severities.1", "value": "5"},
        {"name": "severities.0", "value": "4"},
        {"name": "tags.0.tag", "value": "a"},
        {"name": "tags.0.value", "value": "b"},
        {"name": "groupids", "value": "1"},
        {"name": "groupids", "value": "2"},
        {"name": "show_lines", "value": "10"},
    ])
    assert fields["severities"] == ["4", "5"]
    assert fields["tags"] == [{"tag": "a", "value": "b"}]
    assert fields["groupids"] == ["1", "2"]
    assert fields["show_lines"] == "10"


def test_settings_defaults_and_params():
    s = build_settings({"name": "w", "fields": []})
    assert s.show_lines == 25 and s.sort_triggers == 4 and not s.show_suppressed
    params = build_problem_params(s, None, 100)
    assert params["recent"] is False
    assert params["suppressed"] is False and params["symptom"] is False
    assert "acknowledged" not in params and "groupids" not in params


def test_settings_ack_and_tags():
    s = build_settings({"fields": [
        {"name": "acknowledgement_status", "value": "1"},
        {"name": "show_suppressed", "value": "1"},
        {"name": "evaltype", "value": "2"},
        {"name": "tags.0.tag", "value": "env"},
        {"name": "tags.0.operator", "value": "1"},
        {"name": "tags.0.value", "value": "prod"},
    ]})
    params = build_problem_params(s, ["3"], 10)
    assert params["acknowledged"] is False
    assert "suppressed" not in params
    assert params["evaltype"] == 2
    assert params["tags"] == [{"tag": "env", "operator": 1, "value": "prod"}]
    assert params["groupids"] == ["3"]
    legacy = build_settings({"fields": [{"name": "unacknowledged", "value": "1"}]})
    assert build_problem_params(legacy, None, 1)["acknowledged"] is False


@pytest.mark.parametrize(
    "fields",
    [
        [{"name": "hostids._reference", "value": "ABCDE._hostids"}],
        [{"name": "sort_triggers", "value": "7"}],
        [{"name": "brand_new_field", "value": "1"}],
        [{"name": "show", "value": "9"}],
    ],
)
def test_unsupported_settings(fields):
    with pytest.raises(PreparationError) as info:
        build_settings({"fields": fields})
    assert info.value.category == Category.WIDGET_UNSUPPORTED


def test_unknown_fields_can_be_ignored():
    build_settings({"fields": [{"name": "brand_new_field", "value": "1"}]}, ignore_unknown=True)


def test_find_first_problems_widget():
    def w(wid, kind, x, y):
        return {"widgetid": wid, "type": kind, "x": str(x), "y": str(y)}

    dash = [{"pages": [
        {"widgets": [w("1", "clock", 0, 0)]},
        {"widgets": [w("2", "problems", 12, 0), w("3", "problems", 0, 5), w("4", "problems", 0, 0)]},
        {"widgets": [w("5", "problems", 0, 0)]},
    ]}]
    # First page with a Problems widget, then top-left on that page.
    assert find_problems_widget(dash)["widgetid"] == "4"
    for empty in ([], [{"pages": [{"widgets": [w("1", "clock", 0, 0)]}]}]):
        with pytest.raises(PreparationError) as info:
            find_problems_widget(empty)
        assert info.value.category == Category.WIDGET_NOT_FOUND


def test_subgroups():
    groups = [{"groupid": "1", "name": "Linux"}, {"groupid": "2", "name": "Linux/DB"}, {"groupid": "3", "name": "Linuxes"}]
    assert expand_subgroups(["1"], groups) == ["1", "2"]


def test_sorting():
    p = [
        {"eventid": "1", "clock": 100, "severity": 2, "name": "b", "host_name": "h2"},
        {"eventid": "2", "clock": 300, "severity": 5, "name": "a", "host_name": "h1"},
        {"eventid": "3", "clock": 200, "severity": 5, "name": "c", "host_name": "h1"},
    ]
    ids = lambda items: [x["eventid"] for x in items]  # noqa: E731
    assert ids(sort_problems(p, 4)) == ["2", "3", "1"]
    assert ids(sort_problems(p, SORT_TIME_ASC)) == ["1", "3", "2"]
    assert ids(sort_problems(p, SORT_SEVERITY_DESC)) == ["2", "3", "1"]
    assert ids(sort_problems(p, SORT_NAME_DESC)) == ["3", "1", "2"]
    assert ids(sort_problems(p, SORT_HOST_ASC)) == ["2", "3", "1"]


def test_display_tags():
    s = build_settings({"fields": [
        {"name": "show_tags", "value": "2"},
        {"name": "tag_name_format", "value": "1"},
        {"name": "tag_priority", "value": "zone, scope"},
    ]})
    tags = [{"tag": "app", "value": "x"}, {"tag": "scope", "value": "s"}, {"tag": "zone", "value": "z"}]
    assert display_tags(tags, s) == ["zon: z", "sco: s"]


def test_classify_rpc_error():
    assert isinstance(classify_rpc_error({"code": -32602, "data": "Not authorized."}), ZabbixAuthError)
    assert isinstance(classify_rpc_error({"code": -32602, "data": "Session terminated, re-login, please."}), ZabbixAuthError)
    assert isinstance(classify_rpc_error({"code": -32500, "data": "No permissions."}), ZabbixApiError)
    assert isinstance(classify_rpc_error({"code": -32603, "data": "x"}), ZabbixTemporaryError)


def test_extract_emails():
    medias = [
        {"sendto": ["a@x.com", "A@x.com"], "active": "0"},
        {"sendto": ["off@x.com"], "active": "1"},
        {"sendto": "+123456", "active": "0"},
        {"sendto": "s@x.com", "active": "0"},
    ]
    assert extract_emails(medias) == ["a@x.com", "s@x.com"]


def client(fake: FakeZabbix, attempts: int = 3) -> ZabbixClient:
    async def no_sleep(_):
        return None

    return ZabbixClient(
        "https://zabbix.example.com", retry=RetryConfig(attempts=attempts, initial_delay=0), transport=fake.transport(),
        sleep=no_sleep,
    )


async def test_validate_user():
    fake = FakeZabbix()
    c = client(fake)
    identity = await validate_user(c, TOKEN, "Alice@Example.com")
    assert identity.userid == "7"
    with pytest.raises(PreparationError) as info:
        await validate_user(c, OTHER_TOKEN, "alice@example.com")
    assert info.value.category == Category.EMAIL_NOT_OWNED
    with pytest.raises(PreparationError) as info:
        await validate_user(c, "c" * 64, "alice@example.com")
    assert info.value.category == Category.ZABBIX_AUTH
    fake.users[TOKEN]["usrgrps"][0]["users_status"] = "1"
    with pytest.raises(PreparationError) as info:
        await validate_user(c, TOKEN, "alice@example.com")
    assert info.value.category == Category.ZABBIX_USER_DISABLED
    # The token never travels in the request body except for checkAuthentication.
    assert all(token in (None, TOKEN, OTHER_TOKEN) for _, _, token in fake.calls)


async def test_retry_on_temporary_errors():
    fake = FakeZabbix()
    fake.fail_next = ["timeout", "502"]
    c = client(fake, attempts=3)
    assert (await validate_user(c, TOKEN, "alice@example.com")).userid == "7"
    fake.fail_next = ["timeout", "timeout", "timeout"]
    with pytest.raises(PreparationError) as info:
        await validate_user(c, TOKEN, "alice@example.com")
    assert info.value.category == Category.ZABBIX_UNAVAILABLE


async def test_fetch_problems_limit_and_truncation():
    fake = FakeZabbix()
    c = client(fake)
    ps = await fetch_problems(c, TOKEN, "7", parse_widget_url("https://z/zabbix.php?dashboardid=12"), 100, now=5000)
    assert ps.widget_name == "DB problems"
    assert [p["eventid"] for p in ps.problems] == ["103", "102"]
    assert ps.total == 3 and ps.total_exact and ps.truncated
    first = ps.problems[0]
    assert first["host_name"] == "app2" and first["duration_seconds"] == 2000
    assert first["url"] == "https://zabbix.example.com/tr_events.php?triggerid=13&eventid=103"
    method, params, _ = next(c for c in fake.calls if c[0] == "problem.get")
    assert params["severities"] == [4, 5]
    assert params["tags"] == [{"tag": "service", "operator": 0, "value": "db"}]


async def test_fetch_problems_cap_marks_incomplete():
    fake = FakeZabbix()
    ps = await fetch_problems(client(fake), TOKEN, "7", parse_widget_url("https://z/zabbix.php?dashboardid=12"), 2, now=5000)
    assert not ps.total_exact and ps.truncated


async def test_unmonitored_trigger_skipped():
    fake = FakeZabbix()
    del fake.triggers["13"]
    ps = await fetch_problems(client(fake), TOKEN, "7", parse_widget_url("https://z/zabbix.php?dashboardid=12"), 100, now=5000)
    assert ps.total == 2


async def test_missing_dashboard():
    fake = FakeZabbix()
    with pytest.raises(PreparationError) as info:
        await fetch_problems(client(fake), TOKEN, "7", parse_widget_url("https://z/zabbix.php?dashboardid=99"), 100)
    assert info.value.category == Category.WIDGET_NOT_FOUND


@pytest.mark.parametrize(
    "widget, minimum, expected",
    [
        ([], None, []),
        ([], 4, [4, 5]),
        ([2, 4], 3, [4]),
        ([0, 5], 0, [0, 5]),
        ([2], 4, []),
    ],
)
def test_apply_min_severity(widget, minimum, expected):
    s = WidgetSettings(severities=list(widget))
    assert apply_overrides(s, Overrides(min_severity=minimum)) is (bool(expected) or minimum is None)
    assert s.severities == expected


@pytest.mark.parametrize(
    "status, show_suppressed, overrides, result, expected_status, expected_suppressed",
    [
        (ACK_ALL, True, Overrides(), True, ACK_ALL, True),
        (ACK_ALL, True, Overrides(hide_acknowledged=True), True, ACK_UNACK, True),
        (ACK_UNACK, False, Overrides(hide_acknowledged=True), True, ACK_UNACK, False),
        (ACK_ACK, False, Overrides(hide_acknowledged=True), False, ACK_ACK, False),
        (ACK_ALL, True, Overrides(hide_suppressed=True), True, ACK_ALL, False),
        (ACK_ALL, False, Overrides(hide_suppressed=True), True, ACK_ALL, False),
    ],
)
def test_apply_ack_and_suppression_overrides(status, show_suppressed, overrides, result, expected_status,
                                             expected_suppressed):
    s = WidgetSettings(acknowledgement_status=status, show_suppressed=show_suppressed)
    assert apply_overrides(s, overrides) is result
    assert s.acknowledgement_status == expected_status and s.show_suppressed == expected_suppressed


async def test_fetch_problems_min_severity():
    fake = FakeZabbix()  # widget severities: High, Disaster
    c = client(fake)
    ref = parse_widget_url("https://z/zabbix.php?dashboardid=12")

    def problem_calls():
        return [params for method, params, _ in fake.calls if method == "problem.get"]

    ps = await fetch_problems(c, TOKEN, "7", ref, 100, overrides=Overrides(min_severity=5))
    assert [p["name"] for p in ps.problems] == ["Disk full"] and ps.overrides.min_severity == 5
    assert problem_calls()[-1]["severities"] == [5]
    ps = await fetch_problems(c, TOKEN, "7", ref, 100, overrides=Overrides(min_severity=3))
    assert ps.total == 3 and problem_calls()[-1]["severities"] == [4, 5]
    # Nothing left after narrowing: no problem.get call.
    fake.fields = [f for f in fake.fields if f["name"] != "severities.1"]
    count = len(problem_calls())
    ps = await fetch_problems(c, TOKEN, "7", ref, 100, overrides=Overrides(min_severity=5))
    assert ps.problems == [] and ps.total == 0 and len(problem_calls()) == count
