# Zabbix Digest

A self-hosted service that sends a daily digest of active Zabbix problems.

- Each subscription points to a Problems widget in Zabbix. Filters, sorting and the line limit come from the widget.
- Every user reads Zabbix with an own API token, so Zabbix permissions apply.
- Digests are HTML emails. Other channels can be added as adapters or external programs.
- Core works alone with a YAML file. The web frontend is optional and adds self-service registration and subscription management.

Target: Zabbix 7.0 LTS, Python 3.10 or newer.

Requirements: `docs/AI-brief.md`. Implementation decisions and open questions: `docs/decisions.md`. Zabbix details to verify: `docs/zabbix-notes.md`.

## Components

- Core (`src/core/`, `python -m core`) - scheduler, Zabbix integration, transports, internal API, metrics. Port 8081.
- Web (`src/web/`, `python -m web`) - FastAPI backend (`src/web/src`) and the Vue application (`src/web/ui`). Port 8080. Optional.
- Shared code (`src/common/`) - configuration loading, logging, SMTP, YAML helpers. Used by both components.

Each component directory has its own `requirements.txt`, `src/`, `tests/` and `Dockerfile`. Component requirements include `src/common/requirements.txt`. Docker images are built from the repository root, because they need `src/common/`.

## Quick start: Core without frontend

```sh
python3 -m venv .venv
.venv/bin/pip install -r src/core/requirements.txt
export PYTHONPATH=src/common/src:src/core/src

cp examples/core.sample.yaml core.yaml      # set zabbix.url, smtp.*, paths
cp examples/users.sample.yaml users.yaml    # set emails, tokens, widget links
chmod 600 users.yaml

python -m core -c core.yaml check-config
python -m core -c core.yaml validate-users users.yaml
python -m core -c core.yaml check-user alice@example.com
python -m core -c core.yaml preview a1b2 -o preview.html
python -m core -c core.yaml test-send a1b2
python -m core -c core.yaml run
```

Create the Zabbix API token in Zabbix: User settings - API tokens. The email of a user must be configured as an enabled email media of the Zabbix user.

## Quick start: with frontend

This is demo with Docker Compose, fake Zabbix API with demo users and dashboard, and Mailpit.

`examples/docker-compose.yml` runs Core and web. Run the commands from the repository root.

### Demo without Zabbix

```sh
docker compose -f examples/docker-compose.yml --profile demo up --build
```

- The `demo` profile adds a static Zabbix API stub (`examples/zabbix-stub/`) at http://zabbix.localhost:8090 and Mailpit as SMTP test server. Without `examples/.env` the compose file defaults to them.
- UI: http://localhost:8080, test mailbox (Mailpit): http://localhost:8025, Core: http://localhost:8081.
- The defaults contain placeholder keys and stub tokens. Use them for local tests only.
- Use `--profile demo` also for `logs`, `restart`, `down` of the demo services.

With the stub:

- The stub has two users ()`x-stub-users` in `examples/docker-compose.yml`), sign in with specified tokens:
  - `demo@example.com` (regular user)
    - token: `0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef`
  - `admin@example.com` (frontend administrator, the default of `ADMIN_EMAILS`)
    - token: `fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210`
- Users and tokens are set by `STUB_USERS` (`email=token` pairs separated by spaces or commas). All stub users see the same dashboard and problems.
- As `admin@example.com`, use impersonation to act as `demo@example.com` after demo has registered.
- Add a subscription with the link `http://zabbix.localhost:8090/zabbix.php?action=dashboard.view&dashboardid=1`.
- Preview shows 5 of 6 matching problems. Problems hidden by the widget filters (severity, host group, tag, suppression) are described in `examples/zabbix-stub/data.yaml`.
- Send now: the "Send now" button of the subscription, or `docker compose -f examples/docker-compose.yml exec core python -m core once --force`. The mail appears in Mailpit.
- Problem and widget links in the mail open a placeholder page of the stub.
- Edit `examples/zabbix-stub/data.yaml` to try other widget settings, then `docker compose -f examples/docker-compose.yml --profile demo restart zabbix-stub`.
- When the environment sets an HTTP proxy that does not exclude `zabbix.localhost`, set `PROXY_USE_ZABBIX=false` (e.g. in `examples/.env`), so Core reaches the stub directly.
- `*.localhost` names resolve to 127.0.0.1 in current browsers; if not, add `127.0.0.1 zabbix.localhost` to the hosts file.

Mailpit:

- It accepts all mail of Core (digests, test messages, login links, unsubscribe confirmations) for any recipient and delivers nothing outside. All messages are in one inbox at http://localhost:8025; use the search field to filter, for example `to:demo@example.com` or `subject:digest`.
- It logs every received message (`MP_VERBOSE`): `docker compose -f examples/docker-compose.yml --profile demo logs -f mailpit`.
- It keeps up to 500 messages until its container is recreated (`down`, `up --force-recreate`). Old messages are deleted first.

### Real Zabbix and SMTP server

```sh
cp examples/env.sample examples/.env
docker compose -f examples/docker-compose.yml up --build -d
```

- `examples/.env` is read by docker compose automatically (it is the project directory) and is ignored by git. Keep it secret.
- Set `ZABBIX_URL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (`none`, `starttls`, `tls`), `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`.
- Set `INTERNAL_API_KEY` and `SESSION_SECRET` to random values (`openssl rand -hex 32`). Empty values fall back to the demo placeholders.
- `PUBLIC_URL` is the address users open; it is used in login and unsubscribe links. With an https address behind a reverse proxy set `COOKIE_SECURE=true`.
- `ADMIN_EMAILS` - frontend administrators, comma separated. `TZ` - time zone of delivery times.
- Without `--profile demo` the stub and Mailpit do not start.

## CLI (Core)

All commands take `-c <core.yaml>` (or `CORE_CONFIG`).

- `run` - run the service.
- `once [-s ID ...]` - process due subscriptions once and exit. Updates state like a normal run.
- `once --force [-s ID ...]` - send digests of all enabled subscriptions (or the listed ones) now, even if their time has not come. Real Zabbix data and real delivery; empty reports are not sent; preparation errors send an error notification. The schedule state is not changed, so the regular digest of the day is still sent. Prints `{id: result}`; exit code 1 if any delivery failed.
- `preview ID [-o file.html]` - render a digest with real data. Nothing is sent, state is not changed.
- `test-send ID` - send a test message with synthetic data. State is not changed.
- `check-user EMAIL` - check token, Zabbix user status and email ownership.
- `validate-users [FILE]` - validate a user configuration without contacting Zabbix.
- `check-config` - validate the Core configuration including transports.
- `new-id [--file users.yaml] [--length 4]` - print an unused subscription ID.
- `new-token` - print a random token (for keys or unsubscribe tokens).

Exit codes: 0 success, 1 operation failed, 2 configuration error.

## Configuration

### Rules for Core and web

- YAML file (`-c`, or `CORE_CONFIG` / `WEB_CONFIG`) plus environment variables.
- Precedence: environment variables, then YAML, then defaults.
- Nested keys use `__`: `SMTP__HOST` is `smtp.host`, `SCHEDULER__MAX_WORKERS` is `scheduler.max_workers`. Names are not case-sensitive.
- Lists and maps in environment variables are JSON, e.g. `TRANSPORTS__EMAIL__RETRY='{"attempts": 5}'`. `ADMIN_EMAILS` also accepts a comma-separated list.
- Durations: seconds or `30s`, `5m`, `2h`, `1d`, `1h30m`.
- Unknown keys and invalid values stop the start with a clear error. Error texts do not include values.
- Changes require a restart.

Full annotated examples: `examples/core.sample.yaml`, `examples/web.sample.yaml`.

### Core settings

- `zabbix` - `url`, `timeout`, `verify_tls`, `ca_file`, `max_concurrent_requests` (global limit for all Zabbix calls), `retry`, `max_problems`, `ignore_unknown_widget_fields`.
- `users` - `source` (`file` or `http`), `file`, `cache_file`, `poll_interval`, `startup_retry`, `wait_max_delay`, `request_timeout`.
- `frontend` - `url` (internal URL of the web backend, http mode), `public_url` (for unsubscribe links).
- `internal_api_key` - shared key for `/internal/*`, at least 16 characters, required in http mode.
- `proxy_use_internal` (default false), `proxy_use_zabbix` (default true) - use proxies from the environment for requests to the frontend and to the Zabbix API. See Common failures.
- `http` - `enabled`, `host`, `port`.
- `scheduler` - `timezone`, `tick_interval`, `max_workers`, `delivery_window` (max 12h), `retry_interval`, `cleanup_interval`, `shutdown_timeout`.
- `state_dir` - directory for state files.
- `transports` - named transport instances, see `docs/adapters.md`.
- `smtp` - default SMTP settings, see `docs/adapters.md`. Also used for the emails requested by the web frontend (login links, unsubscribe confirmations).
- `messages` - `header`, `footer`, `unsubscribe_text` (used when there is no frontend).
- `log` - `level`, `format` (`json` or `text`), `file`, `max_bytes`, `backup_count`.
- `sentry_dsn`, `sentry_environment`.

Time zone: `scheduler.timezone`, else the `TZ` environment variable, else the system zone, else UTC. All subscription times use this zone. The UI shows it next to the time field.

### Web settings

- `internal_api_key`, `session_secret` (at least 32 characters) - required.
- No SMTP settings: Core sends all messages, including login links and unsubscribe confirmations. An old `smtp` section is rejected at start (`smtp: Extra inputs are not permitted`); remove it.
- `proxy_use_internal` - default false: requests to Core ignore proxies from the environment.
- `users_file` - the authoritative user configuration in http mode.
- `state_dir` - one-time login link bookkeeping.
- `core_url`, `core_timeout`, `public_url`.
- `help_url` (env `HELP_URL`) - optional link to user help (http/https). The UI shows a Help button right of the navigation buttons and below the login forms. It is a real link: it opens in a new tab and its URL can be copied from the browser context menu. Empty: no button.
- `admin_emails` - administrators who may impersonate users.
- `max_subscriptions_per_user` (0: no limit), `subscription_id_length` (default 4).
- `login_link_ttl`, `impersonation_ttl`, `session_max_age`, `cookie_secure`, `trust_proxy_headers`.
- `rate_limits` - per-IP and per-email limits for login and unsubscribe endpoints.
- `http`, `static_dir`, `log`, `sentry_dsn`, `sentry_browser_dsn`, `sentry_environment`.

### User configuration (`users.yaml`)

```yaml
revision: 1
users:
  - email: alice@example.com
    zabbix_token: <token>
    subscriptions:
      - id: a1b2
        name: Database servers
        widget_url: https://zabbix.example.com/zabbix.php?action=dashboard.view&dashboardid=12
        time: "08:30"
        transport: email
        enabled: true
        min_severity: High
        hide_acknowledged: true
```

- `email` - unique key and delivery address.
- `zabbix_token` - the user's API token.
- `subscriptions[].id` - 1-32 lowercase hex characters, unique in the whole file.
- `name` - optional title. Shown as `Name (#id)` in the UI; without a name only `#id`.
- `widget_url` - dashboard link. The first Problems widget of the dashboard is used: the first page that has one, and on that page the top-left widget.
- `time` - daily time `HH:MM`. Quote it.
- `transport` - transport name, default `email`.
- `enabled` - default true.
- `min_severity` - optional, `0`-`5` or a severity name (`Not classified`, `Information`, `Warning`, `Average`, `High`, `Disaster`). Narrows the widget severity filter: only problems of this severity and higher are included. It never adds severities the widget does not select. When nothing is left (for example the widget selects only Warning and `min_severity` is High), the digest is always empty and not sent.
- `hide_acknowledged` - optional, default false (widget setting). When true, acknowledged problems are excluded. A widget that shows only acknowledged problems then gives an always empty digest.
- `hide_suppressed` - optional, default false (widget setting). When true, suppressed problems are excluded even if the widget shows them.
- Fields written by the web frontend: `zabbix_userid`, `generation`, `session_version`, `unsubscribe_token`, `transport_options`.

The whole file is secret: keep it at mode 0600 and do not share it.

## Behavior

### Digests

- One digest per subscription per day, at the subscription time.
- Changing the time after today's digest was sent: if the new time is still ahead today, the digest is sent again today at the new time; if it has already passed, the next digest comes tomorrow.
- Only problems active at preparation time. No digest is sent when nothing matches; the day counts as done.
- Columns: time, severity, host, problem (link to event details), duration; tags as configured in the widget. Operational data is not shown.
- The problem table comes first, so previews and mobile notifications show problem data. After the table: the truncation warning, `Total N active problems`, generation time, `Source: widget "..." on dashboard "..."`, subscription filters. The subscription name is in the subject only.
- The plain text part has one problem per line (`time | severity | host | problem | ...`), without the table header row.
- Test messages have the same layout with a TEST banner on top and synthetic problems.
- When more problems match than the widget shows, the digest says "Showing N of M" and links to the widget.
- With filter overrides the digest lists them, e.g. "Subscription filters: minimal severity High; acknowledged problems hidden."
- Every message has an unsubscribe footer: a link with the frontend, the `messages.unsubscribe_text` without it.

### Errors

- Invalid token, disabled Zabbix user, email no longer in the user's media, missing widget, unsupported widget settings: an error notification is sent at the scheduled time, every day, until the problem is fixed. The subscription stays enabled and resumes automatically.
- Zabbix temporarily unavailable: requests are retried; if it does not recover, an error notification is sent for that day.
- Delivery failure (SMTP down, sender error): retried with backoff; then retried every `retry_interval` inside `delivery_window`. No error notification is sent; check logs and metrics.

### Startup

- File mode: the user file must exist and be valid, otherwise Core exits with code 2.
- File mode: the user file is read only at startup. Restart Core to apply changes; validate the file first with `validate-users`.
- Http mode: Core fetches the configuration from the web backend (`users.startup_retry`). If that fails, it starts with the cache file. Without a valid cache it keeps retrying and does not schedule digests; `/health/ready` returns 503 meanwhile.
- After restart, slots of the current window that were not completed are processed (recovery). Older slots are not.

### Cache recovery (http mode)

- Every accepted configuration is written to `users.cache_file` (mode 0600, atomic).
- An invalid configuration from the web backend never replaces the current one or the cache.
- To reset, stop Core and delete the cache; Core then waits for the web backend.

### Graceful shutdown

- SIGTERM/SIGINT: no new work starts, preparation is interrupted, active deliveries continue up to `scheduler.shutdown_timeout`.
- An interrupted slot is processed again after restart if it is still inside the delivery window. A message accepted by the SMTP server right before a crash may be sent twice; this is accepted.

## Web frontend

- Registration: enter a Zabbix API token, choose one of the email addresses of your Zabbix user.
- Later logins: a one-time link by email (valid 15 minutes by default, one use). If the saved token became invalid, the UI asks for a new token.
- Sessions are signed cookies. They stay valid until logout, "Log out all sessions", profile deletion or a change of `session_secret`.
- Subscriptions: create, edit, enable/disable, delete, preview (email only, real data, nothing sent), send now (real digest, delivered immediately; an empty report is not sent; errors are shown instead of sending an error notification; the regular schedule is not affected), test send (synthetic data, real delivery).
- Profile: change email to another address of the same Zabbix user, replace the token, log out all sessions, delete the profile (type `DELETE`).
- Unsubscribe: the link in a digest opens a confirmation page. Only the button click disables the subscription; a confirmation email is sent once.
- Administrators (`admin_emails`) see an "Impersonate" form on the profile page. During impersonation the normal UI is shown with a "Stop impersonation" button. Impersonation ends after `impersonation_ttl`, when the browser closes, or when the target logs out all sessions. Audit logs record the administrator as the actor.

## Operations

### Moving from file mode to frontend mode

- Stop Core.
- Copy the user file to the web backend `users_file` location (mode 0600). Keep `revision`.
- Start the web backend.
- Change Core to `users.source: http`, set `frontend.url`, `frontend.public_url` and `internal_api_key` (same value in both components).
- Start Core. It fetches the configuration and writes its cache.
- Existing users log in with their Zabbix token once. Unsubscribe tokens are created when a subscription is saved in the UI.

### Manual session invalidation

- Increase `session_version` of the user in `users.yaml` (http mode: the web backend `users_file`) and increase `revision`.
- In http mode the web backend sees the change immediately; Core picks it up at the next poll.
- To invalidate all sessions of all users, change `session_secret` and restart the web backend.

### Logs

- JSON to stdout by default. `log.format: text` for plain text. `log.file` writes a rotating file.
- Access log: one line per HTTP request (`core.access`, `web.access`), e.g. `POST /api/auth/token 502 8ms client 10.0.0.5`. Health checks (`/health/*`) and `/metrics` are not logged. Tokens in paths are shown as `{token}`. `log.access: false` (env `LOG__ACCESS=false`) turns it off.
- Every error shown to a user or returned by the Core internal API is logged with its technical cause; web also logs what Core answered (`core answered HTTP 502 zabbix_error: ...`).
- `LOG_FORCE_TEXT_STDOUT=true` forces plain text to stdout and overrides the configuration.
- Context fields: `component`, `operation`, `user` (email), `subscription_id`, `transport`, `error_category`, `actor`, `target`. In text format they follow the message in brackets: `digest delivered (1 of 1 problems) [operation=scheduled user=alice@example.com subscription_id=277c transport=email]`.
- Every record about a user names `user`; about a subscription, `user` and `subscription_id`. This includes Zabbix request retries and delivery attempts. During impersonation web records also have `actor` (the administrator).
- Records without a known user: token checks before an email is chosen (only the token is known, and it is never logged), configuration loading, startup.
- Audit records use the logger `zabbix_digest.audit` and the message prefix `audit:`.
- Tokens, passwords and keys are never logged on purpose; known secret values are also masked in log records.
- At start both components log their main settings as `config: ...` lines: Zabbix URL, user configuration source, URLs of the other component, SMTP server, state directory, time zone, transports, HTTP listener, proxy settings from the environment. Secrets are not included.
- HTTP errors name the request (`GET http://web:8080/internal/config`), the proxy when one is used, the HTTP status, the `Server` header and the start of the response body. URLs are shown without query string and credentials. SMTP errors name the server (`host:port (security)`).

### Metrics

Core (`zabbix_digest_core_*`):

- `executions_total{result}` - delivered, empty, error_notified, failed, interrupted.
- `deliveries_total{transport,type,result}`, `delivery_attempt_failures_total{transport}`, `transport_retries_total{transport}`, `delivery_duration_seconds{transport}`.
- `preparation_errors_total{category}`, `zabbix_api_errors_total{kind}`, `zabbix_api_retries_total`.
- `processing_duration_seconds`, `active_workers`, `scheduler_ready`.
- `config_revision`, `config_validation_failures_total{source}`, `config_sync_failures_total`.
- `operations_total{operation,result}` (preview, test send, send now, identify, forced once), `unexpected_errors_total`.

Web (`zabbix_digest_web_*`):

- `http_requests_total{method,route,status}`, `http_request_duration_seconds{method,route}`.
- `auth_failures_total{reason}`, `login_link_requests_total{result}`, `rate_limited_total{limit}`.
- `config_validation_failures_total`, `config_save_failures_total{reason}`, `core_failures_total{operation}`, `unexpected_errors_total`.

### Health checks

- `/health/live` and `/health/ready` on both components. Zabbix and SMTP outages do not make them fail.

### Sentry

- Set `sentry_dsn` (Core, web backend) and `sentry_browser_dsn` (Vue). Without a DSN Sentry is off.
- Only unexpected errors are sent. Tracing, profiling and replay are off. Secrets are filtered in `before_send`.

### Common failures

- `invalid configuration` at start - the message names the key; fix it and restart.
- Error notification `zabbix_auth` - the token is revoked or expired; the user creates a new token (UI: Profile - Replace token).
- `email_not_owned` - the address was removed from the user's Zabbix media.
- `widget_not_found` - the dashboard was deleted, is not shared with the user, or has no Problems widget.
- `widget_unsupported` - the widget uses a setting the service does not support (for example data from another widget).
- Web UI "service temporarily unavailable" on save - Core is not reachable; nothing was saved; Core keeps running with the last configuration.
- HTTP 409 on save - the configuration changed meanwhile; press Save again.
- `core_failures_total` grows - check `core_url` and `internal_api_key` in both components.
- Proxies: each kind of outgoing HTTP request either uses the proxy settings of the environment (`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY`) as is, or ignores them. `proxy_use_internal` (Core and web, env `PROXY_USE_INTERNAL`, default false) - requests between Core and web. `proxy_use_zabbix` (Core, env `PROXY_USE_ZABBIX`, default true) - requests to the Zabbix API; set it to false when Zabbix (or the demo stub) is reachable only directly. Docker can inject proxy variables into containers from `~/.docker/config.json`.
- An HTTP error with `via proxy ...` in the message or a proxy `Server` header - the request went through a proxy from the environment. For Zabbix, check `NO_PROXY`.

### TLS certificates

All outgoing TLS connections (Zabbix API, SMTP of Core, Core <-> web over https) choose trusted CA certificates in the same order:

- `ca_file` of the connection: `zabbix.ca_file`, `smtp.ca_file` (env `ZABBIX__CA_FILE`, `SMTP__CA_FILE`).
- `SSL_CERT_FILE` and/or `SSL_CERT_DIR` from the environment.
- The certifi bundle (public CAs only). This is the default.

`verify_tls: false` disables the check for that connection. A missing CA file or directory stops the start with a configuration error. The startup log shows the source per connection (`trusted CAs: ...`).

Internal CA on Debian/Ubuntu-based images (`python:3.12-slim` is one): the system bundle `/etc/ssl/certs/ca-certificates.crt` contains the public CAs and the CAs added with `update-ca-certificates`.

- The base image already contains the internal CA in the system store: set in `examples/.env`

  ```sh
  SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  ```

- Add the CA while building the image (`BUILD_EXTRA_CMD`, see Docker images), e.g. download it from an internal URL:

  ```sh
  BUILD_EXTRA_CMD=apt-get update && apt-get install -y ca-certificates curl && curl -fsS -o /usr/local/share/ca-certificates/corp-ca.crt https://pki.example.com/corp-ca.crt && update-ca-certificates && apt-get clean && rm -rf /var/lib/apt/lists/*
  SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  ```

- Without rebuilding: mount the host bundle into the containers (Compose override file) and point `SSL_CERT_FILE` to it:

  ```yaml
  # examples/docker-compose.override.yml
  services:
    core:
      volumes: ["/etc/ssl/certs/ca-certificates.crt:/etc/ssl/host-ca.crt:ro"]
    web:
      volumes: ["/etc/ssl/certs/ca-certificates.crt:/etc/ssl/host-ca.crt:ro"]
  ```

  Set `SSL_CERT_FILE=/etc/ssl/host-ca.crt` in `examples/.env` and pass both files, because with `-f` compose does not load the override file by itself: `docker compose -f examples/docker-compose.yml -f examples/docker-compose.override.yml up -d`.

## Security notes

- Keep `users.yaml`, the Core cache, the state directories and configuration files readable only by the service user.
- Keep `/internal/*` and the Core port private. Put the web backend behind HTTPS and keep `cookie_secure: true`.
- External sender commands come only from the administrator configuration and are started without a shell.
- Stored Zabbix tokens are never returned to the browser.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r tests/requirements.txt
tests/run.sh                       # all tests; extra arguments go to pytest, e.g. tests/run.sh -k widget

npm --prefix src/web/ui ci
npm --prefix src/web/ui run build      # output: src/web/ui/dist
npm --prefix src/web/ui run dev        # dev server, proxies /api to localhost:8080
```

Python 3.10 check:

```sh
docker run --rm -v "$PWD":/src:ro -w /src python:3.10-slim \
  sh -c "pip install -q -r tests/requirements.txt && PYTHON=python tests/run.sh -q -p no:cacheprovider"
```

Tests use an in-memory Zabbix fake (`src/core/tests/fake_zabbix.py`), a local SMTP server (aiosmtpd) and in-process HTTP between Core and web. No real Zabbix is needed.

Docker images: `src/core/Dockerfile`, `src/web/Dockerfile` (build context: repository root).

- Python packages are installed into a virtual environment `/opt/venv`, so the images also build on bases that forbid system-wide pip installs (PEP 668, e.g. Ubuntu 24.04).
- Build arguments, all optional:
  - `BASE_IMAGE_PYTHON` (default `python:3.12-slim`) - runtime base image of both images. It must provide `python3` with the `venv` module (Debian/Ubuntu package `python3-venv`), or `BUILD_EXTRA_CMD` must install it.
  - `BASE_IMAGE_NODE` (default `node:22-slim`) - UI build stage of the web image.
  - `BUILD_EXTRA_CMD`, `BUILD_EXTRA_CMD_UI` - shell command run as root right after `FROM` of the runtime stage or the UI build stage. Skipped when empty.
  - `APP_UID`, `APP_GID` (default `10001`, `10001`) - numeric user and group `zabbix-digest` the service runs as.
- The images run as a non-root user with a numeric `USER` (`10001:10001`), so Kubernetes `runAsNonRoot: true` works. `/data` is owned by that user and group with mode `0770`.
- Kubernetes: set `fsGroup` to the group that must own the volume (usually `APP_GID`). The service writes its files with mode `0600` and creates the state directory with mode `0700`, so the process must keep the same UID across restarts: do not change `runAsUser` for an existing volume.
- With `examples/docker-compose.yml` set them in `examples/.env` (see `examples/env.sample`), then `docker compose -f examples/docker-compose.yml build`.
- Directly: `docker build -f src/web/Dockerfile --build-arg BASE_IMAGE_PYTHON=ubuntu:24.04 --build-arg 'BUILD_EXTRA_CMD=apt-get update && apt-get install -y python3 python3-venv' .`

Versions: the project uses Semantic Versioning (`src/common/src/common/version.py`). The message schema version and the user configuration revision are independent of it.
