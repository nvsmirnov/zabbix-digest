"""Problems widget URL parsing and Zabbix link construction."""

from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit


class WidgetUrlError(ValueError):
    pass


@dataclass(frozen=True)
class WidgetRef:
    dashboardid: str


def _single(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    if len(set(values)) > 1:
        raise WidgetUrlError(f"parameter '{name}' is given more than once")
    return values[0]


def parse_widget_url(url: str) -> WidgetRef:
    """Parse a dashboard URL.

    Supported form (Zabbix 7.0 global dashboards):
      <zabbix>/zabbix.php?action=dashboard.view&dashboardid=<id>

    The digest uses the first Problems widget of the dashboard (see find_problems_widget).
    """
    if not isinstance(url, str) or not url.strip():
        raise WidgetUrlError("the link is empty")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise WidgetUrlError("the link must be an http or https URL")
    query = parse_qs(parts.query, keep_blank_values=True)
    action = _single(query, "action")
    if action is not None and action != "dashboard.view":
        raise WidgetUrlError("the link is not a dashboard link (action=dashboard.view expected)")
    if parts.path.rsplit("/", 1)[-1] not in ("zabbix.php", ""):
        raise WidgetUrlError("the link is not a Zabbix dashboard link")
    dashboardid = _single(query, "dashboardid")
    if not dashboardid or not dashboardid.isdigit():
        raise WidgetUrlError("the link has no numeric dashboardid parameter")
    return WidgetRef(dashboardid=dashboardid)


def event_url(base_url: str, triggerid: str, eventid: str) -> str:
    return f"{base_url}/tr_events.php?" + urlencode({"triggerid": triggerid, "eventid": eventid})


def dashboard_url(base_url: str, dashboardid: str) -> str:
    return f"{base_url}/zabbix.php?" + urlencode({"action": "dashboard.view", "dashboardid": dashboardid})
