"""In-memory fake of the Zabbix JSON-RPC API for tests."""

import json

import httpx

TOKEN = "a" * 64
OTHER_TOKEN = "b" * 64


class FakeZabbix:
    def __init__(self) -> None:
        self.users = {
            TOKEN: {
                "userid": "7",
                "username": "alice",
                "medias": [{"mediatypeid": "1", "sendto": ["alice@example.com"], "active": "0"}],
                "usrgrps": [{"usrgrpid": "1", "users_status": "0"}],
            },
            OTHER_TOKEN: {
                "userid": "8",
                "username": "bob",
                "medias": [{"mediatypeid": "1", "sendto": ["bob@example.com", "b2@example.com"], "active": "0"}],
                "usrgrps": [{"usrgrpid": "1", "users_status": "0"}],
            },
        }
        self.fields = [
            {"type": "0", "name": "show_lines", "value": "2"},
            {"type": "0", "name": "severities.0", "value": "4"},
            {"type": "0", "name": "severities.1", "value": "5"},
            {"type": "0", "name": "show_tags", "value": "3"},
            {"type": "1", "name": "tags.0.tag", "value": "service"},
            {"type": "0", "name": "tags.0.operator", "value": "0"},
            {"type": "1", "name": "tags.0.value", "value": "db"},
        ]
        self.widgets = None  # override full widget list
        self.problems = [
            {"eventid": "101", "objectid": "11", "clock": "1000", "name": "Disk full", "severity": "5",
             "acknowledged": "0", "suppressed": "0", "tags": [{"tag": "service", "value": "db"}]},
            {"eventid": "102", "objectid": "12", "clock": "2000", "name": "CPU high", "severity": "4",
             "acknowledged": "1", "suppressed": "0", "tags": []},
            {"eventid": "103", "objectid": "13", "clock": "3000", "name": "Memory low", "severity": "4",
             "acknowledged": "0", "suppressed": "0", "tags": []},
        ]
        self.triggers = {
            "11": {"triggerid": "11", "description": "Disk full", "hosts": [{"hostid": "1", "name": "db1"}]},
            "12": {"triggerid": "12", "description": "CPU high", "hosts": [{"hostid": "2", "name": "app1"}]},
            "13": {"triggerid": "13", "description": "Memory low", "hosts": [{"hostid": "3", "name": "app2"}]},
        }
        self.groups = [{"groupid": "1", "name": "Linux"}, {"groupid": "2", "name": "Linux/DB"}]
        self.calls: list[tuple[str, dict, str | None]] = []
        self.fail_next: list[str] = []  # "timeout", "500"
        self.down = False

    def widget_list(self) -> list[dict]:
        if self.widgets is not None:
            return self.widgets
        return [
            {"widgetid": "55", "type": "clock", "name": "", "fields": []},
            {"widgetid": "56", "type": "problems", "name": "DB problems", "fields": self.fields},
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        if self.fail_next:
            kind = self.fail_next.pop(0)
            if kind == "timeout":
                raise httpx.ReadTimeout("timeout")
            return httpx.Response(int(kind))
        body = json.loads(request.content)
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else None
        method, params = body["method"], body["params"]
        self.calls.append((method, params, token))

        def ok(result):
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": body["id"]})

        def err(data):
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "error": {"code": -32602, "message": "Invalid params.", "data": data}, "id": body["id"]}
            )

        if method == "user.checkAuthentication":
            user = self.users.get(params.get("token"))
            if not user:
                return err("Not authorized.")
            return ok({"userid": user["userid"], "username": user["username"]})
        user = self.users.get(token)
        if user is None:
            return err("Not authorized.")
        if method == "user.get":
            return ok([user])
        if method == "dashboard.get":
            if params["dashboardids"] != ["12"]:
                return ok([])
            return ok([{"dashboardid": "12", "name": "Ops", "pages": [{"widgets": self.widget_list()}]}])
        if method == "hostgroup.get":
            return ok(self.groups)
        if method == "problem.get":
            result = list(self.problems)
            if "severities" in params:
                result = [p for p in result if int(p["severity"]) in params["severities"]]
            if params.get("acknowledged") is False:
                result = [p for p in result if p["acknowledged"] == "0"]
            result.sort(key=lambda p: -int(p["eventid"]))
            return ok(result[: params.get("limit", 10**6)])
        if method == "trigger.get":
            return ok({k: v for k, v in self.triggers.items() if k in params["triggerids"]})
        return err(f"unknown method {method}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


WIDGET_URL = "https://zabbix.example.com/zabbix.php?action=dashboard.view&dashboardid=12"
