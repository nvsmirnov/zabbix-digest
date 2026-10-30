# Zabbix 7.0 integration notes

The code was tested against an in-memory fake of the Zabbix API only (`src/core/tests/fake_zabbix.py`).
The example stub (`examples/zabbix-stub/`) follows the same assumptions and is not a reference of Zabbix behavior.
Everything below must be checked against a real Zabbix 7.0 before production use.
Items are marked "verify" where the behavior is based on Zabbix source knowledge, not on a test.

## API access

- Endpoint: `<zabbix.url>/api_jsonrpc.php`.
- Authentication: `Authorization: Bearer <token>` header (supported since 6.4; the `auth` body field is not used).
- Errors with "Not authorized", "Session terminated", "API token expired" are treated as invalid credentials.
- Error code -32603 and "Database error" are treated as temporary. Connection errors, timeouts, HTTP 5xx, 408 and 429 are temporary too.

## User identity

- `user.checkAuthentication {token}` returns the token owner (`userid`, `username`).
- `user.get {userids, selectMedias: [mediatypeid, sendto, active], selectUsrgrps: [usrgrpid, users_status]}` with the user's own token.
- The user is disabled when any of the user's groups has `users_status = 1`. Verify: a disabled user may already fail at `checkAuthentication`.
- Eligible emails: media with `active = 0`; `sendto` as a list (email media) or a string that contains `@`. The media type is not checked, because ordinary users may not have access to `mediatype.get`. Verify.
- Verify that users with the "User" role can read their own media through `user.get`.

## Widget lookup

- `dashboard.get {dashboardids, selectPages: [dashboard_pageid, widgets]}` with the user's token. An inaccessible dashboard returns an empty result.
- Widget type `problems`. The first Problems widget is used: first page in API order that has one, then the smallest `y`, then the smallest `x`. Verify that `dashboard.get` returns pages in display order and widgets with `x`/`y`.
- Fields come as `{type, name, value}`. Multi-value fields are `name.N`, tag rows are `tags.N.tag`, `tags.N.operator`, `tags.N.value`. The repeated-name form of older versions is also accepted.
- Fields with default values are not stored by Zabbix. Defaults used: `show=1`, `show_lines=25`, `sort_triggers=4`, `show_tags=0`, `tag_name_format=0`, `show_suppressed=0`, `acknowledgement_status=0`, `acknowledged_by_me=0`, `show_symptoms=0`, `evaltype=0`.
- A field `<name>._reference` (data taken from another widget or the dashboard) is rejected as unsupported.
- Unknown fields are rejected unless `zabbix.ignore_unknown_widget_fields: true`.

## Field mapping to problem.get

| Widget field | API request | Notes |
| --- | --- | --- |
| `show` | `recent: false` always | Digests contain only active problems; "Recent problems" and "History" modes are reduced to active problems. |
| `groupids` | `groupids` with subgroups | Subgroups are groups named `<parent>/...`. Verify. |
| `exclude_groupids` | `groupids` = selected (or all) minus excluded with subgroups | A host in both an excluded and another group stays visible. Verify against the widget. |
| `hostids` | `hostids` | |
| `problem` | `search: {name}` | |
| `severities` | `severities` | |
| `evaltype`, `tags` | `evaltype`, `tags` | Operators 0-5 are passed as is. |
| `acknowledgement_status` | `acknowledged: false` (1) or `true` (2) | 6.x field `unacknowledged=1` is mapped to status 1. |
| `acknowledged_by_me` | `selectAcknowledges` + filter on own `userid` with action bit 2 | Verify. |
| `show_suppressed=0` | `suppressed: false` | |
| `show_symptoms=0` | `symptom: false` | |
| `show_lines` | applied in Core after sorting | |

- Problems are fetched with `limit = zabbix.max_problems + 1`. More results mark the report incomplete.
- `trigger.get {triggerids, selectHosts, monitored: true}` gives host names. Problems of triggers that are not monitored or not visible are dropped, as the Zabbix UI does. Verify whether `skipDependent` is also needed.
- `show_opdata` is ignored and `opdata` is not requested (see `docs/decisions.md`). Unverified: `problem.get` returns `opdata` only for triggers with an operational data expression, while the Zabbix UI shows latest item values otherwise.
- Severity names are the Zabbix defaults; custom severity names from the Zabbix settings are not read.

## Sorting

- Done in Core for all supported values of `sort_triggers`: time desc/asc (4/3), severity desc/asc (1/11), problem name asc/desc (13/14), host asc/desc (2/12).
- Secondary order is newest first. Verify that the widget uses the same tie breaking.

## Links

- Event details: `<zabbix.url>/tr_events.php?triggerid=<id>&eventid=<id>`. Verify for 7.0.
- Widget link: the URL stored in the subscription.
