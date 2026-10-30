"""Static Zabbix API stub for local testing of the digest service.

Implements only the JSON-RPC methods used by Core, with data from a YAML file.
It is not a reference of Zabbix behavior; see docs/zabbix-notes.md.

Environment:
  STUB_USERS  Users as "email=token" pairs separated by spaces or commas (required).
              User IDs are assigned in order starting from 3; the username is the
              part of the email before "@". All users see the same data.
  STUB_DATA   Data file (default: data.yaml next to this script).
  STUB_PORT   Listen port (default: 8090).

Requires PyYAML (available in the Core image).
"""

import html
import json
import logging
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("zabbix-stub")

# problem.get tag operators.
TAG_LIKE, TAG_EQUAL, TAG_NOT_LIKE, TAG_NOT_EQUAL, TAG_EXISTS, TAG_NOT_EXISTS = range(6)
EVALTYPE_OR = 2


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: str) -> None:
        super().__init__(data)
        self.code, self.message, self.data = code, message, data


def not_authorized() -> RpcError:
    return RpcError(-32602, "Invalid params.", "Not authorized.")


def widget_fields(fields: dict[str, Any]) -> list[dict]:
    """Convert a compact mapping into the API field list.

    Lists become "name.N", lists of mappings become "name.N.key" (tag rows).
    Type: 0 for numbers, 1 for strings.
    """
    result = []

    def add(name: str, value: Any) -> None:
        kind = "0" if isinstance(value, int) else "1"
        result.append({"type": kind, "name": name, "value": str(value)})

    for name, value in (fields or {}).items():
        if isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    for key, sub in item.items():
                        add(f"{name}.{i}.{key}", sub)
                else:
                    add(f"{name}.{i}", item)
        else:
            add(name, value)
    return result


def tag_matches(tags: list[dict], flt: dict) -> bool:
    name, op, value = str(flt.get("tag", "")), int(flt.get("operator", 0)), str(flt.get("value", ""))
    values = [str(t.get("value", "")) for t in tags if str(t.get("tag")) == name]
    if op == TAG_EXISTS:
        return bool(values)
    if op == TAG_NOT_EXISTS:
        return not values
    if op == TAG_LIKE:
        return any(value.lower() in v.lower() for v in values)
    if op == TAG_EQUAL:
        return value in values
    if op == TAG_NOT_LIKE:
        return not any(value.lower() in v.lower() for v in values)
    if op == TAG_NOT_EQUAL:
        return value not in values
    raise RpcError(-32602, "Invalid params.", f"unsupported tag operator {op}")


def tags_match(tags: list[dict], filters: list[dict], evaltype: int) -> bool:
    if evaltype == EVALTYPE_OR:
        return any(tag_matches(tags, f) for f in filters)
    # And/Or: OR between filters of the same tag name, AND between names.
    by_name: dict[str, list[dict]] = {}
    for f in filters:
        by_name.setdefault(str(f.get("tag", "")), []).append(f)
    return all(any(tag_matches(tags, f) for f in group) for group in by_name.values())


class Stub:
    def __init__(self, data: dict, users: list[tuple[str, str]]) -> None:
        # token -> user
        self.users = {
            token: {"userid": str(3 + i), "username": email.split("@")[0], "email": email}
            for i, (email, token) in enumerate(users)
        }
        self.dashboards = data.get("dashboards") or []
        self.groups = data.get("hostgroups") or []
        self.hosts = {str(h["hostid"]): h for h in data.get("hosts") or []}
        self.problems = data.get("problems") or []

    def call(self, method: str, params: Any, token: str | None) -> Any:
        if method == "user.checkAuthentication":
            user = self.users.get((params or {}).get("token"))
            if user is None:
                raise not_authorized()
            return {"userid": user["userid"], "username": user["username"]}
        user = self.users.get(token or "")
        if user is None:
            raise not_authorized()
        handler = getattr(self, "m_" + method.replace(".", "_"), None)
        if handler is None:
            raise RpcError(-32601, "Method not found.", f"Incorrect API \"{method}\".")
        return handler(params or {}, user)

    def m_user_get(self, params: dict, user: dict) -> list:
        # Only the token owner is visible.
        userids = [str(u) for u in params.get("userids") or []]
        if userids and user["userid"] not in userids:
            return []
        return [{
            "userid": user["userid"],
            "username": user["username"],
            "medias": [{"mediatypeid": "1", "sendto": [user["email"]], "active": "0"}],
            "usrgrps": [{"usrgrpid": "7", "users_status": "0"}],
        }]

    def m_dashboard_get(self, params: dict, user: dict) -> list:
        ids = [str(i) for i in params.get("dashboardids") or []]
        result = []
        for d in self.dashboards:
            if ids and str(d["dashboardid"]) not in ids:
                continue
            pages = []
            for p, page in enumerate(d.get("pages") or []):
                widgets = []
                for w, widget in enumerate(page.get("widgets") or []):
                    widgets.append({
                        "widgetid": str(widget.get("widgetid", f"{p + 1}{w + 1}")),
                        "type": widget["type"],
                        "name": widget.get("name", ""),
                        "x": str(widget.get("x", 0)),
                        "y": str(widget.get("y", 0)),
                        "width": str(widget.get("width", 36)),
                        "height": str(widget.get("height", 5)),
                        "view_mode": "0",
                        "fields": widget_fields(widget.get("fields") or {}),
                    })
                pages.append({"dashboard_pageid": str(p + 1), "widgets": widgets})
            result.append({"dashboardid": str(d["dashboardid"]), "name": d["name"], "pages": pages})
        return result

    def m_hostgroup_get(self, params: dict, user: dict) -> list:
        return [{"groupid": str(g["groupid"]), "name": g["name"]} for g in self.groups]

    def _hostids(self, problem: dict) -> list[str]:
        return [str(h) for h in problem.get("hostids") or []]

    def m_problem_get(self, params: dict, user: dict) -> list:
        now = int(time.time())
        groupids = {str(g) for g in params.get("groupids") or []}
        hostids = {str(h) for h in params.get("hostids") or []}
        severities = params.get("severities")
        search = str((params.get("search") or {}).get("name") or "").lower()
        result = []
        for p in self.problems:
            hosts = self._hostids(p)
            if severities is not None and int(p["severity"]) not in [int(s) for s in severities]:
                continue
            if "acknowledged" in params and bool(p.get("acknowledged")) != bool(params["acknowledged"]):
                continue
            if params.get("suppressed") is False and p.get("suppressed"):
                continue
            if params.get("groupids") is not None and not any(
                groupids & {str(g) for g in self.hosts.get(h, {}).get("groups") or []} for h in hosts
            ):
                continue
            if hostids and not hostids & set(hosts):
                continue
            if search and search not in str(p["name"]).lower():
                continue
            tags = p.get("tags") or []
            if params.get("tags") and not tags_match(tags, params["tags"], int(params.get("evaltype", 0))):
                continue
            result.append({
                "eventid": str(p["eventid"]),
                "objectid": str(p["triggerid"]),
                "clock": str(now - int(p.get("age", 0))),
                "name": p["name"],
                "severity": str(p["severity"]),
                "acknowledged": "1" if p.get("acknowledged") else "0",
                "suppressed": "1" if p.get("suppressed") else "0",
                "cause_eventid": "0",
                "tags": [{"tag": str(t["tag"]), "value": str(t.get("value", ""))} for t in tags],
                "acknowledges": [],
            })
        result.sort(key=lambda r: -int(r["eventid"]))
        return result[: int(params.get("limit") or len(result))]

    def m_trigger_get(self, params: dict, user: dict) -> Any:
        ids = {str(i) for i in params.get("triggerids") or []}
        triggers = {}
        for p in self.problems:
            triggerid = str(p["triggerid"])
            if triggerid not in ids:
                continue
            hosts = [{"hostid": h, "name": self.hosts.get(h, {}).get("name", h)} for h in self._hostids(p)]
            triggers[triggerid] = {"triggerid": triggerid, "description": p["name"], "hosts": hosts}
        return triggers if params.get("preservekeys") else list(triggers.values())


def make_handler(stub: Stub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            text = html.escape(self.path)
            body = f"<!doctype html><title>Zabbix stub</title><p>Zabbix stub. Requested: <code>{text}</code></p>"
            self.send(200, "text/html; charset=utf-8", body.encode())

        def do_POST(self) -> None:
            if self.path.split("?")[0].rstrip("/").rsplit("/", 1)[-1] != "api_jsonrpc.php":
                self.send(404, "text/plain", b"not found")
                return
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                method, params, rid = request["method"], request.get("params"), request.get("id")
            except (ValueError, KeyError, TypeError):
                self.send(400, "text/plain", b"bad request")
                return
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else None
            try:
                response = {"jsonrpc": "2.0", "result": stub.call(method, params, token), "id": rid}
                log.info("%s ok", method)
            except RpcError as exc:
                response = {"jsonrpc": "2.0", "error": {"code": exc.code, "message": exc.message, "data": exc.data},
                            "id": rid}
                log.info("%s error: %s", method, exc.data)
            self.send(200, "application/json", json.dumps(response).encode())

    return Handler


def parse_users(value: str) -> list[tuple[str, str]]:
    users = []
    for item in value.replace(",", " ").split():
        email, sep, token = item.partition("=")
        if not sep or "@" not in email or not token:
            raise ValueError("STUB_USERS: expected email=token pairs")
        users.append((email, token))
    if len({t for _, t in users}) != len(users):
        raise ValueError("STUB_USERS: tokens must be unique")
    return users


def load_stub(path: str, users: list[tuple[str, str]]) -> Stub:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Stub(data, users)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        users = parse_users(os.environ.get("STUB_USERS", ""))
    except ValueError as exc:
        sys.exit(str(exc))
    if not users:
        sys.exit("STUB_USERS is required")
    path = os.environ.get("STUB_DATA", str(Path(__file__).with_name("data.yaml")))
    port = int(os.environ.get("STUB_PORT", "8090"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(load_stub(path, users)))
    log.info("listening on port %d, data %s, users: %s", port, path, ", ".join(e for e, _ in users))
    # PID 1 in a container ignores SIGTERM without a handler.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
