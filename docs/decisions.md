# Implementation decisions and open questions

This file records how the implementation resolves points that `docs/AI-brief.md` leaves open.
Items marked "needs agreement" change or narrow documented behavior and must be confirmed by a human.

## Widget identification (agreed)

- Zabbix 7.0 has no standard link to a single dashboard widget. A copied dashboard link contains `dashboardid` only.
- Supported link: `<zabbix>/zabbix.php?action=dashboard.view&dashboardid=<id>`. Other query parameters are ignored.
- The digest uses the first Problems widget of the dashboard: the first page (in `dashboard.get` order) that has a Problems widget, and on that page the top-left one (smallest `y`, then smallest `x`).
- A dashboard without a Problems widget gives `widget_not_found`.
- Template (host) dashboards are not supported.

## Filter overrides (agreed)

- Requested explicitly as an exception to "no per-subscription filter overrides" in `docs/AI-brief.md`.
- Fields: `min_severity` (0-5 or a severity name, stored as a number), `hide_acknowledged`, `hide_suppressed` (booleans, default false = widget setting).
- Overrides only narrow the widget filter:
  - `min_severity`: the widget severities that are >= `min_severity`. An empty widget selection means all severities.
  - `hide_acknowledged`: widget "all" becomes "unacknowledged"; widget "acknowledged only" gives an empty report.
  - `hide_suppressed`: suppressed problems are excluded.
- When no problem can match, `problem.get` is not called and the report is empty (not sent, run successful).
- The digest message carries `digest.filters`; the email lists the active overrides under the source link.
- The web frontend stores only overrides that differ from the default.

## Operational data (agreed)

- Not shown. The column was empty in digests while the Zabbix dashboard showed values. Probable cause (not verified): `problem.get` returns `opdata` only for triggers with an operational data expression, while the Zabbix UI otherwise shows latest item values. The column was removed instead of reproducing that logic.
- The widget field `show_opdata` is ignored.

## Example Zabbix stub

- `examples/zabbix-stub/stub.py` is a static JSON-RPC stub for the compose example. It implements only the methods and parameters Core uses.
- `src/core/tests/test_zabbix_stub.py` runs the real Core client against it, so the stub follows changes in Core requests.

## Temporary Zabbix errors

- Individual API requests are retried with backoff (`zabbix.retry`).
- When retries are exhausted, the execution sends an error notification and is marked completed for the slot, as for any other preparation error. It is not retried later in the same window.
- Scheduled retries inside the delivery window apply only when delivery itself failed (digest or error notification).

## Scheduling and state

- Slot: the local calendar date in the Core time zone. Slot of date D starts at D + subscription time.
- A slot is due while `start <= now < start + delivery_window` and `last_completed_slot < D`.
- Slots of today and yesterday are checked, so a window crossing midnight works. `delivery_window` is limited to 12h.
- After a failed attempt inside the slot, the next attempt waits `scheduler.retry_interval`.
- Nonexistent local time (DST forward): the slot starts shifted forward by the gap (02:30 runs at 03:30).
- Ambiguous local time (DST back): the first occurrence is used.
- Changing the time after the day's slot was completed (agreed, changed on request): if the new time is still ahead today, the slot is reopened and the digest is sent again at the new time; if it has passed, nothing more is sent that day. The decision is made when Core applies the new configuration (`schedule.apply_time_change`); the state keeps `reopened_slot`.
- A reopened slot whose time is changed again to a moment already passed is closed again, so no immediate second digest is sent.
- Not handled: a digest still in progress when the time is changed completes the day; the new time then does not apply until the next day.
- In file mode the configuration is read only at startup. Core has no previous configuration to compare with, so the reopening rule does not apply there: a completed day stays completed after a restart.
- Changing the time to a moment already passed today, but inside the window, runs the slot immediately.
- Execution results in state: `delivered`, `empty`, `error_notified` (failure, slot completed), `failed` (slot not completed).
- `attempt_started_at` is set during an attempt. A value left after a crash only shows that an attempt did not finish; no special recovery is done.
- Preview, test send and send now never change state.
- Send now (web) differs from `once --force` (CLI): preparation errors are returned to the user instead of sending an error notification, because the user is waiting for the answer.
- `once --force` sends digests of enabled subscriptions immediately, using the regular pipeline (empty reports skipped, error notifications on preparation errors). It does not change state, so it does not replace or suppress the scheduled digest of the day. Disabled subscriptions are skipped even when listed with `-s`.

## Graceful shutdown

- On SIGTERM or SIGINT, no new tasks start, preparation is cancelled, and active deliveries continue up to `scheduler.shutdown_timeout`.
- Delivery retries stop once shutdown begins.

## User configuration

- Users are a list, the email is the key (case-insensitive unique).
- Extra fields for the web frontend: `zabbix_userid`, `generation`, `session_version`, `unsubscribe_token`.
- Unquoted YAML times such as `8:30` are parsed by YAML 1.1 as numbers; they are converted back to `08:30`.
- In file mode Core reads `users.yaml` only at startup (agreed). Changes require a restart.

## Frontend synchronization and conflicts

- The web backend serializes changes with a lock. Each change: read, modify a copy, revision + 1, Core validation, check that the revision on disk is unchanged, atomic write, notify Core.
- A revision change on disk during the operation (manual edit, another process) returns HTTP 409. The UI keeps the entered values and the user presses Save again.
- In http mode `users.yaml` is rewritten by the web backend. Comments in the file are not preserved.

## Web authentication

- One-time login links are signed tokens with a short validity. Used link IDs are stored in `<state_dir>/used_login_links.yaml` until they expire. The link is used only after an explicit click on the login page, so mail scanners do not consume it.
- A token login for an existing profile is accepted only for the same Zabbix user: `zabbix_userid` must match. For profiles without `zabbix_userid`, the stored token is checked; if it is invalid, the ownership of the email address is the proof.
- Login emails and unsubscribe confirmations are sent by Core (`POST /internal/mail/*`) through its global `smtp` section, not through transports: a login link must go to the email address. The web backend has no SMTP settings.
- Mutating `/api/*` requests require the header `X-Requested-With: zabbix-digest` (CSRF protection).

## Configuration snapshot

- A task that already started may finish after the subscription was changed or deleted. This follows the configuration snapshot rule of the MVP.

## Repository

- Documentation lives in `docs/`: requirements `docs/AI-brief.md`, user documentation `docs/AI-readme.md`, agent rules `docs/AGENTS.md`. The root `AGENTS.md` is a one-line pointer, because agent tools look for it in the root.
- Python 3.10 works. `pydantic-settings` is pinned to 2.15.0 because 2.16+ requires Python 3.11.

## Proxies (agreed)

- Requests between Core and web ignore `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY` by default, because environments often set a `NO_PROXY` that does not cover internal service names such as `web`.
- `proxy_use_internal: true` (Core and web, env `PROXY_USE_INTERNAL`) uses the environment proxy settings as is.
- Only proxies are disabled, not `trust_env`, so `SSL_CERT_FILE`/`SSL_CERT_DIR` keep working. Implementation: `common.http.proxy_options` (httpx mounts for `http://`, `https://` and `all://`).
- Requests to the Zabbix API: `proxy_use_zabbix` (Core, env `PROXY_USE_ZABBIX`), default true. False ignores the environment proxy settings, e.g. for the demo stub or a Zabbix in the same network. Core does not try to detect which case applies.

## TLS trust (agreed)

- One rule for all TLS clients (`common.tls.ssl_context`): connection `ca_file`, then `SSL_CERT_FILE`/`SSL_CERT_DIR`, then certifi.
- Before, httpx (Zabbix) used certifi and SMTP used the OpenSSL system defaults. Now both use certifi by default; SMTP changes from the system store to certifi when no CA is configured.
- `pip_system_certs` (global `ssl` patch at interpreter start) and `setuptools_scm` (build tool) are not used.
- `zabbix.ca_file` was added for symmetry with `smtp.ca_file`.
