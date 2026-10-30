"""Retrieve active problems that match a Problems widget."""

import time
from dataclasses import dataclass

from ..errors import Category, PreparationError, ZabbixError, zabbix_to_preparation
from .client import ZabbixClient
from .urls import WidgetRef, WidgetUrlError, event_url, parse_widget_url
from .widget import (
    ACK_ACK,
    SEVERITY_NAMES,
    WidgetSettings,
    acknowledged_by,
    build_problem_params,
    build_settings,
    display_tags,
    expand_subgroups,
    find_problems_widget,
    Overrides,
    apply_overrides,
    sort_problems,
)


@dataclass
class ProblemSet:
    widget_name: str
    settings: WidgetSettings
    problems: list[dict]
    total: int
    total_exact: bool
    dashboard_name: str = ""
    overrides: Overrides = Overrides()

    @property
    def truncated(self) -> bool:
        return self.total > len(self.problems) or not self.total_exact


async def read_widget(client: ZabbixClient, token: str, ref: WidgetRef, ignore_unknown: bool) -> tuple[dict, dict]:
    dashboards = await client.call(
        "dashboard.get",
        {
            "output": ["dashboardid", "name"],
            "dashboardids": [ref.dashboardid],
            "selectPages": ["dashboard_pageid", "widgets"],
        },
        token=token,
    )
    widget = find_problems_widget(dashboards or [])
    build_settings(widget, ignore_unknown)  # fail early on unsupported settings
    return dashboards[0], widget


async def fetch_problems(
    client: ZabbixClient,
    token: str,
    userid: str,
    ref: WidgetRef,
    max_problems: int,
    ignore_unknown: bool = False,
    now: float | None = None,
    overrides: Overrides = Overrides(),
) -> ProblemSet:
    try:
        return await _fetch(client, token, userid, ref, max_problems, ignore_unknown, now, overrides)
    except ZabbixError as exc:
        raise zabbix_to_preparation(exc) from None


async def _fetch(client, token, userid, ref, max_problems, ignore_unknown, now, overrides) -> ProblemSet:
    now = time.time() if now is None else now
    dashboard, widget = await read_widget(client, token, ref, ignore_unknown)
    s = build_settings(widget, ignore_unknown)

    def empty() -> ProblemSet:
        return ProblemSet(widget_name=s.name, settings=s, problems=[], total=0, total_exact=True,
                          dashboard_name=str(dashboard.get("name", "")), overrides=overrides)

    if not apply_overrides(s, overrides):
        return empty()

    groupids: list[str] | None = None
    if s.groupids or s.exclude_groupids:
        all_groups = await client.call("hostgroup.get", {"output": ["groupid", "name"]}, token=token)
        selected = expand_subgroups(s.groupids, all_groups) if s.groupids else [str(g["groupid"]) for g in all_groups]
        excluded = set(expand_subgroups(s.exclude_groupids, all_groups)) if s.exclude_groupids else set()
        groupids = [g for g in selected if g not in excluded]
        if not groupids:
            return empty()

    params = build_problem_params(s, groupids, max_problems + 1)
    raw = await client.call("problem.get", params, token=token) or []
    exact = len(raw) <= max_problems
    raw = raw[:max_problems]

    if s.acknowledgement_status == ACK_ACK and s.acknowledged_by_me:
        raw = [p for p in raw if acknowledged_by(p, userid)]

    triggerids = sorted({str(p["objectid"]) for p in raw})
    triggers: dict[str, dict] = {}
    if triggerids:
        result = await client.call(
            "trigger.get",
            {
                "output": ["triggerid", "description"],
                "triggerids": triggerids,
                "selectHosts": ["hostid", "name"],
                "monitored": True,
                "preservekeys": True,
            },
            token=token,
        )
        triggers = {str(k): v for k, v in (result or {}).items()} if isinstance(result, dict) else {
            str(t["triggerid"]): t for t in result or []
        }

    prepared = []
    for p in raw:
        trigger = triggers.get(str(p["objectid"]))
        if trigger is None:
            # Trigger not monitored or not accessible: the Zabbix UI does not show such problems.
            continue
        hosts = sorted(
            ({"hostid": str(h["hostid"]), "name": str(h.get("name", ""))} for h in trigger.get("hosts") or []),
            key=lambda h: h["name"].lower(),
        )
        tags = [{"tag": str(t.get("tag", "")), "value": str(t.get("value", ""))} for t in p.get("tags") or []]
        clock = int(p["clock"])
        severity = int(p.get("severity", 0))
        prepared.append(
            {
                "eventid": str(p["eventid"]),
                "triggerid": str(p["objectid"]),
                "name": str(p.get("name", "")),
                "severity": severity,
                "severity_name": SEVERITY_NAMES[severity] if 0 <= severity < len(SEVERITY_NAMES) else str(severity),
                "clock": clock,
                "duration_seconds": max(0, int(now) - clock),
                "acknowledged": str(p.get("acknowledged", "0")) == "1",
                "suppressed": str(p.get("suppressed", "0")) == "1",
                "hosts": hosts,
                "host_name": ", ".join(h["name"] for h in hosts),
                "tags": tags,
                "tags_display": display_tags(tags, s),
                "url": event_url(client.base_url, str(p["objectid"]), str(p["eventid"])),
            }
        )

    ordered = sort_problems(prepared, s.sort_triggers)
    total = len(ordered)
    return ProblemSet(
        widget_name=s.name,
        settings=s,
        problems=ordered[: s.show_lines],
        total=total,
        total_exact=exact,
        dashboard_name=str(dashboard.get("name", "")),
        overrides=overrides,
    )


def parse_ref(url: str) -> WidgetRef:
    try:
        return parse_widget_url(url)
    except WidgetUrlError as exc:
        raise PreparationError(Category.WIDGET_URL, str(exc)) from None
