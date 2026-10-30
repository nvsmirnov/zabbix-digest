"""Problems widget settings and their translation to Zabbix API requests.

Target: Zabbix 7.0. Widget fields with default values are not stored by Zabbix,
so every field has a default here that matches the Zabbix widget form.
See docs/zabbix-notes.md for the mapping and the items that need verification.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import Category, PreparationError

WIDGET_TYPE_PROBLEMS = "problems"

SEVERITY_NAMES = ["Not classified", "Information", "Warning", "Average", "High", "Disaster"]

# "show" field.
SHOW_RECENT = 1
SHOW_HISTORY = 2
SHOW_PROBLEMS = 3

# "sort_triggers" values supported by the Problems widget (Zabbix defines.inc.php).
SORT_SEVERITY_DESC = 1
SORT_HOST_ASC = 2
SORT_TIME_ASC = 3
SORT_TIME_DESC = 4
SORT_SEVERITY_ASC = 11
SORT_HOST_DESC = 12
SORT_NAME_ASC = 13
SORT_NAME_DESC = 14
SUPPORTED_SORTS = {
    SORT_SEVERITY_DESC, SORT_HOST_ASC, SORT_TIME_ASC, SORT_TIME_DESC,
    SORT_SEVERITY_ASC, SORT_HOST_DESC, SORT_NAME_ASC, SORT_NAME_DESC,
}

# "acknowledgement_status" (7.0).
ACK_ALL = 0
ACK_UNACK = 1
ACK_ACK = 2

# Event acknowledge action bit "acknowledge".
ACTION_ACKNOWLEDGE = 2

# Fields that only affect presentation in the Zabbix UI and are safe to ignore.
# show_opdata: operational data is not shown in digests.
IGNORED_FIELDS = {"rf_rate", "show_timeline", "highlight_row", "reference", "show_opdata"}

KNOWN_FIELDS = IGNORED_FIELDS | {
    "show", "groupids", "exclude_groupids", "hostids", "problem", "severities", "evaltype",
    "tags", "show_tags", "tag_name_format", "tag_priority", "show_symptoms",
    "show_suppressed", "acknowledgement_status", "acknowledged_by_me", "unacknowledged",
    "sort_triggers", "show_lines",
}

_INDEX_RE = re.compile(r"^([a-z_]+)(?:\.(\d+|_reference))?(?:\.([a-z_]+))?$")


def parse_fields(fields: list[dict]) -> dict[str, Any]:
    """Convert the API field list into a dict.

    Zabbix 7.0 stores multi-value fields as "name.N" and tag rows as "tags.N.tag".
    Older versions repeat the same name; both forms are accepted.
    """
    scalars: dict[str, Any] = {}
    lists: dict[str, dict[int, Any]] = {}
    rows: dict[str, dict[int, dict[str, Any]]] = {}
    references: set[str] = set()
    repeated: dict[str, list[Any]] = {}
    for item in fields or []:
        name = str(item.get("name", ""))
        value = item.get("value")
        match = _INDEX_RE.match(name)
        if not match:
            raise PreparationError(Category.WIDGET_UNSUPPORTED, f"unexpected widget field '{name[:60]}'")
        base, index, sub = match.groups()
        if index == "_reference":
            references.add(base)
            continue
        if index is not None and sub is not None:
            rows.setdefault(base, {}).setdefault(int(index), {})[sub] = value
        elif index is not None:
            lists.setdefault(base, {})[int(index)] = value
        else:
            repeated.setdefault(base, []).append(value)
    for base, values in repeated.items():
        scalars[base] = values[0] if len(values) == 1 else values
    result: dict[str, Any] = dict(scalars)
    for base, items in lists.items():
        result[base] = [items[i] for i in sorted(items)]
    for base, items in rows.items():
        result[base] = [items[i] for i in sorted(items)]
    if references:
        result["_references"] = sorted(references)
    return result


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _as_int(fields: dict, name: str, default: int) -> int:
    value = fields.get(name, default)
    if isinstance(value, list):
        raise PreparationError(Category.WIDGET_UNSUPPORTED, f"widget field '{name}' has several values")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise PreparationError(Category.WIDGET_UNSUPPORTED, f"widget field '{name}' is not a number") from None


@dataclass
class TagFilter:
    tag: str
    operator: int
    value: str


@dataclass
class WidgetSettings:
    name: str = ""
    show: int = SHOW_RECENT
    groupids: list[str] = field(default_factory=list)
    exclude_groupids: list[str] = field(default_factory=list)
    hostids: list[str] = field(default_factory=list)
    problem: str = ""
    severities: list[int] = field(default_factory=list)
    evaltype: int = 0
    tags: list[TagFilter] = field(default_factory=list)
    show_tags: int = 0
    tag_name_format: int = 0
    tag_priority: list[str] = field(default_factory=list)
    show_symptoms: bool = False
    show_suppressed: bool = False
    acknowledgement_status: int = ACK_ALL
    acknowledged_by_me: bool = False
    sort_triggers: int = SORT_TIME_DESC
    show_lines: int = 25


def build_settings(widget: dict, ignore_unknown: bool = False) -> WidgetSettings:
    fields = parse_fields(widget.get("fields") or [])
    refs = fields.pop("_references", None)
    if refs:
        raise PreparationError(
            Category.WIDGET_UNSUPPORTED,
            "the widget takes " + ", ".join(refs) + " from another widget or the dashboard; this is not supported",
        )
    unknown = sorted(set(fields) - KNOWN_FIELDS)
    if unknown and not ignore_unknown:
        raise PreparationError(Category.WIDGET_UNSUPPORTED, "unsupported widget settings: " + ", ".join(unknown))

    s = WidgetSettings(name=str(widget.get("name") or ""))
    s.show = _as_int(fields, "show", SHOW_RECENT)
    if s.show not in (SHOW_RECENT, SHOW_HISTORY, SHOW_PROBLEMS):
        raise PreparationError(Category.WIDGET_UNSUPPORTED, f"unsupported 'show' value {s.show}")
    s.groupids = [str(v) for v in _as_list(fields.get("groupids"))]
    s.exclude_groupids = [str(v) for v in _as_list(fields.get("exclude_groupids"))]
    s.hostids = [str(v) for v in _as_list(fields.get("hostids"))]
    s.problem = str(fields.get("problem") or "")
    try:
        s.severities = sorted({int(v) for v in _as_list(fields.get("severities"))})
    except (TypeError, ValueError):
        raise PreparationError(Category.WIDGET_UNSUPPORTED, "widget severities are not numbers") from None
    s.evaltype = _as_int(fields, "evaltype", 0)
    if s.evaltype not in (0, 2):
        raise PreparationError(Category.WIDGET_UNSUPPORTED, f"unsupported tag evaluation type {s.evaltype}")
    for row in _as_list(fields.get("tags")):
        if not isinstance(row, dict):
            raise PreparationError(Category.WIDGET_UNSUPPORTED, "unexpected tag filter format")
        tag = str(row.get("tag") or "")
        operator = int(row.get("operator", 0) or 0)
        if operator not in (0, 1, 2, 3, 4, 5):
            raise PreparationError(Category.WIDGET_UNSUPPORTED, f"unsupported tag operator {operator}")
        if tag == "" and str(row.get("value") or "") == "":
            continue
        s.tags.append(TagFilter(tag=tag, operator=operator, value=str(row.get("value") or "")))
    s.show_tags = _as_int(fields, "show_tags", 0)
    if s.show_tags not in (0, 1, 2, 3):
        raise PreparationError(Category.WIDGET_UNSUPPORTED, f"unsupported 'show_tags' value {s.show_tags}")
    s.tag_name_format = _as_int(fields, "tag_name_format", 0)
    if s.tag_name_format not in (0, 1, 2):
        raise PreparationError(Category.WIDGET_UNSUPPORTED, f"unsupported tag name format {s.tag_name_format}")
    priority = str(fields.get("tag_priority") or "")
    s.tag_priority = [p.strip() for p in priority.split(",") if p.strip()]
    s.show_symptoms = _as_int(fields, "show_symptoms", 0) == 1
    s.show_suppressed = _as_int(fields, "show_suppressed", 0) == 1
    if "acknowledgement_status" in fields:
        s.acknowledgement_status = _as_int(fields, "acknowledgement_status", ACK_ALL)
    elif _as_int(fields, "unacknowledged", 0) == 1:
        # Zabbix 6.x field.
        s.acknowledgement_status = ACK_UNACK
    if s.acknowledgement_status not in (ACK_ALL, ACK_UNACK, ACK_ACK):
        raise PreparationError(
            Category.WIDGET_UNSUPPORTED, f"unsupported acknowledgement status {s.acknowledgement_status}"
        )
    s.acknowledged_by_me = _as_int(fields, "acknowledged_by_me", 0) == 1
    s.sort_triggers = _as_int(fields, "sort_triggers", SORT_TIME_DESC)
    if s.sort_triggers not in SUPPORTED_SORTS:
        raise PreparationError(Category.WIDGET_UNSUPPORTED, f"unsupported sort order {s.sort_triggers}")
    s.show_lines = _as_int(fields, "show_lines", 25)
    if s.show_lines < 1:
        raise PreparationError(Category.WIDGET_UNSUPPORTED, "the widget line limit must be positive")
    return s


@dataclass(frozen=True)
class Overrides:
    """Per-subscription filters. They only narrow the widget filter; defaults keep it as is."""

    min_severity: int | None = None
    hide_acknowledged: bool = False
    hide_suppressed: bool = False


def apply_overrides(s: WidgetSettings, o: Overrides) -> bool:
    """Narrow the widget settings. Returns False when no problem can match."""
    if o.min_severity is not None:
        # An empty widget selection means all severities.
        selected = s.severities or list(range(len(SEVERITY_NAMES)))
        s.severities = [v for v in selected if v >= o.min_severity]
        if not s.severities:
            return False
    if o.hide_acknowledged:
        if s.acknowledgement_status == ACK_ACK:
            return False
        s.acknowledgement_status = ACK_UNACK
    if o.hide_suppressed:
        s.show_suppressed = False
    return True


def _position(widget: dict) -> tuple[int, int]:
    try:
        return int(widget.get("y", 0)), int(widget.get("x", 0))
    except (TypeError, ValueError):
        return 0, 0


def find_problems_widget(dashboards: list[dict]) -> dict:
    """Return the first Problems widget of the dashboard.

    First: the first page (in API order) that has a Problems widget; on that page the
    top-left widget (smallest y, then smallest x).
    """
    if not dashboards:
        raise PreparationError(Category.WIDGET_NOT_FOUND, "the dashboard was not found or is not accessible")
    for page in dashboards[0].get("pages") or []:
        problems = [w for w in page.get("widgets") or [] if w.get("type") == WIDGET_TYPE_PROBLEMS]
        if problems:
            return min(problems, key=_position)
    raise PreparationError(Category.WIDGET_NOT_FOUND, "the dashboard has no Problems widget")


def expand_subgroups(selected: list[str], all_groups: list[dict]) -> list[str]:
    """Add nested groups ("Parent/Child") of the selected groups, as the Zabbix UI does."""
    names = {str(g["groupid"]): str(g.get("name", "")) for g in all_groups}
    result = set(selected)
    prefixes = [names[g] + "/" for g in selected if g in names]
    for groupid, name in names.items():
        if any(name.startswith(p) for p in prefixes):
            result.add(groupid)
    return sorted(result, key=int)


def build_problem_params(s: WidgetSettings, groupids: list[str] | None, limit: int) -> dict:
    params: dict[str, Any] = {
        "output": [
            "eventid", "objectid", "clock", "name", "severity", "acknowledged", "suppressed", "cause_eventid",
        ],
        "source": 0,
        "object": 0,
        # Digests contain only active problems, regardless of the "show" mode.
        "recent": False,
        "selectTags": ["tag", "value"],
        "sortfield": ["eventid"],
        "sortorder": "DESC",
        "limit": limit,
    }
    if groupids is not None:
        params["groupids"] = groupids
    if s.hostids:
        params["hostids"] = s.hostids
    if s.problem:
        params["search"] = {"name": s.problem}
    if s.severities:
        params["severities"] = s.severities
    if s.tags:
        params["evaltype"] = s.evaltype
        params["tags"] = [{"tag": t.tag, "operator": t.operator, "value": t.value} for t in s.tags]
    if s.acknowledgement_status == ACK_UNACK:
        params["acknowledged"] = False
    elif s.acknowledgement_status == ACK_ACK:
        params["acknowledged"] = True
        if s.acknowledged_by_me:
            params["selectAcknowledges"] = ["userid", "action"]
    if not s.show_suppressed:
        params["suppressed"] = False
    if not s.show_symptoms:
        params["symptom"] = False
    return params


def acknowledged_by(problem: dict, userid: str) -> bool:
    for ack in problem.get("acknowledges") or []:
        try:
            action = int(ack.get("action", 0))
        except (TypeError, ValueError):
            continue
        if str(ack.get("userid")) == userid and action & ACTION_ACKNOWLEDGE:
            return True
    return False


def format_tag(tag: str, value: str, fmt: int) -> str:
    if fmt == 2:
        return value
    name = tag if fmt == 0 else tag[:3]
    return f"{name}: {value}" if value != "" else name


def display_tags(tags: list[dict], s: WidgetSettings) -> list[str]:
    """Tags shown in the widget: priority tags first, then the rest; at most show_tags."""
    if s.show_tags == 0:
        return []
    ordered: list[dict] = []
    rest = list(tags)
    for name in s.tag_priority:
        for t in [t for t in rest if t.get("tag") == name]:
            ordered.append(t)
            rest.remove(t)
    ordered.extend(sorted(rest, key=lambda t: (str(t.get("tag")), str(t.get("value")))))
    return [format_tag(str(t.get("tag", "")), str(t.get("value", "")), s.tag_name_format) for t in ordered[: s.show_tags]]


def sort_problems(problems: list[dict], sort: int) -> list[dict]:
    """Sort prepared problem dicts. Secondary order: newest first, then event ID."""

    def recent_key(p: dict) -> tuple:
        return (-int(p["clock"]), -int(p["eventid"]))

    if sort == SORT_TIME_DESC:
        return sorted(problems, key=recent_key)
    if sort == SORT_TIME_ASC:
        return sorted(problems, key=lambda p: (int(p["clock"]), int(p["eventid"])))
    if sort in (SORT_SEVERITY_DESC, SORT_SEVERITY_ASC):
        sign = -1 if sort == SORT_SEVERITY_DESC else 1
        return sorted(problems, key=lambda p: (sign * int(p["severity"]),) + recent_key(p))
    if sort in (SORT_NAME_ASC, SORT_NAME_DESC, SORT_HOST_ASC, SORT_HOST_DESC):
        field_name = "name" if sort in (SORT_NAME_ASC, SORT_NAME_DESC) else "host_name"
        result = sorted(problems, key=recent_key)
        return sorted(result, key=lambda p: str(p[field_name]).lower(), reverse=sort in (SORT_NAME_DESC, SORT_HOST_DESC))
    raise PreparationError(Category.WIDGET_UNSUPPORTED, f"unsupported sort order {sort}")
