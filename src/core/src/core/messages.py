"""Structured message contract (schema version 1). See docs/adapters.md."""

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import CoreSettings
from .errors import PreparationError
from .users import Subscription, User
from .zabbix.problems import SEVERITY_NAMES, ProblemSet
from .zabbix.widget import Overrides, WidgetSettings

SCHEMA_VERSION = 1


def _iso(ts: float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).isoformat(timespec="seconds")


def unsubscribe_info(settings: CoreSettings, sub: Subscription) -> dict:
    url = None
    if settings.frontend.public_url and sub.unsubscribe_token:
        url = f"{settings.frontend.public_url}/unsubscribe/{sub.unsubscribe_token}"
    return {"url": url, "text": settings.messages.unsubscribe_text}


def _base(kind: str, settings: CoreSettings, tz: ZoneInfo, user: User, sub: Subscription, now: float) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "type": kind,
        "generated_at": _iso(now, tz),
        "timezone": tz.key,
        "recipient": {"email": user.email},
        "subscription": {
            "id": sub.id,
            "name": sub.name,
            "transport": sub.transport,
            "options": dict(sub.transport_options),
        },
        "header": settings.messages.header,
        "footer": settings.messages.footer,
        "unsubscribe": unsubscribe_info(settings, sub),
        "links": {"widget": sub.widget_url, "zabbix": settings.zabbix.url},
    }


def _problem_out(p: dict, tz: ZoneInfo) -> dict:
    return {
        "eventid": p["eventid"],
        "triggerid": p["triggerid"],
        "name": p["name"],
        "severity": {"value": p["severity"], "name": p["severity_name"]},
        "start_time": _iso(p["clock"], tz),
        "start_timestamp": p["clock"],
        "duration_seconds": p["duration_seconds"],
        "duration": format_duration(p["duration_seconds"]),
        "acknowledged": p["acknowledged"],
        "suppressed": p["suppressed"],
        "hosts": p["hosts"],
        "host_name": p["host_name"],
        "tags": p["tags"],
        "tags_display": p["tags_display"],
        "url": p["url"],
    }


def _display(s: WidgetSettings) -> dict:
    return {"show_tags": s.show_tags > 0}


def _severity(value: int | None) -> dict | None:
    if value is None:
        return None
    return {"value": value, "name": SEVERITY_NAMES[value]}


def _filters(o: Overrides) -> dict:
    return {
        "min_severity": _severity(o.min_severity),
        "hide_acknowledged": o.hide_acknowledged,
        "hide_suppressed": o.hide_suppressed,
    }


def digest_message(
    settings: CoreSettings, tz: ZoneInfo, user: User, sub: Subscription, ps: ProblemSet, now: float
) -> dict:
    msg = _base("digest", settings, tz, user, sub, now)
    msg["digest"] = {
        "widget_name": ps.widget_name,
        "dashboard_name": ps.dashboard_name,
        "problems": [_problem_out(p, tz) for p in ps.problems],
        "shown": len(ps.problems),
        "total": ps.total,
        "total_exact": ps.total_exact,
        "truncated": ps.truncated,
        "display": _display(ps.settings),
        "filters": _filters(ps.overrides),
    }
    return msg


def error_message(
    settings: CoreSettings, tz: ZoneInfo, user: User, sub: Subscription, error: PreparationError, now: float
) -> dict:
    msg = _base("error", settings, tz, user, sub, now)
    msg["error"] = {
        "category": error.category,
        "message": error.user_message,
        "details": error.details,
    }
    return msg


def test_message(settings: CoreSettings, tz: ZoneInfo, user: User, sub: Subscription, now: float) -> dict:
    msg = _base("test", settings, tz, user, sub, now)
    samples = [
        (5, "TEST: Disk is full on /var", "test-host-1", 3 * 3600 + 120, ["service: storage"]),
        (4, "TEST: High CPU load", "test-host-2", 47 * 60, ["service: compute"]),
        (2, "TEST: Certificate expires in 10 days", "test-host-3", 5 * 86400, ["scope: security"]),
    ]
    problems = []
    for i, (sev, name, host, age, tags) in enumerate(samples, start=1):
        clock = int(now) - age
        problems.append(
            _problem_out(
                {
                    "eventid": str(i),
                    "triggerid": str(i),
                    "name": name,
                    "severity": sev,
                    "severity_name": SEVERITY_NAMES[sev],
                    "clock": clock,
                    "duration_seconds": age,
                    "acknowledged": False,
                    "suppressed": False,
                    "hosts": [{"hostid": str(i), "name": host}],
                    "host_name": host,
                    "tags": [{"tag": t.split(": ")[0], "value": t.split(": ")[1]} for t in tags],
                    "tags_display": tags,
                    "url": settings.zabbix.url,
                },
                tz,
            )
        )
    msg["digest"] = {
        "widget_name": "TEST",
        "dashboard_name": "TEST",
        "problems": problems,
        "shown": len(problems),
        "total": len(problems),
        "total_exact": True,
        "truncated": False,
        "display": {"show_tags": True},
        "filters": _filters(sub.overrides),
    }
    return msg


def format_duration(seconds: int) -> str:
    """Zabbix-like duration: "3d 4h 5m", "4h 5m", "5m 10s"."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        parts = [f"{days}d", f"{hours}h", f"{minutes}m"]
    elif hours:
        parts = [f"{hours}h", f"{minutes}m"]
    elif minutes:
        parts = [f"{minutes}m", f"{secs}s"]
    else:
        parts = [f"{secs}s"]
    return " ".join(parts)


def subject_for(msg: dict[str, Any]) -> str:
    name = msg["subscription"].get("name")
    if msg["type"] == "error":
        return "Zabbix digest error" + (f": {name}" if name else "")
    if msg["type"] == "test":
        return "[TEST] Zabbix digest test message" + (f": {name}" if name else "")
    d = msg["digest"]
    count = f"{d['total']}{'+' if not d['total_exact'] else ''} active problem{'s' if d['total'] != 1 else ''}"
    return "Zabbix digest" + (f": {name}" if name else "") + f" ({count})"
