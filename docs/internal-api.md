# HTTP API

## Common endpoints (Core and web backend)

- `GET /health/live` - 200 while the process responds. No external checks.
- `GET /health/ready` - Core: 200 when a valid user configuration is loaded and the scheduler runs. Web: 200 when the user configuration directory is writable. Otherwise 503.
- `GET /metrics` - Prometheus metrics.

Health endpoints need no authentication and return no sensitive data.

## Internal API

All `/internal/*` requests require the header `X-Internal-Api-Key: <internal_api_key>`. The same key is used in both directions.
These endpoints must not be reachable from the internet. Use HTTPS when the traffic crosses an untrusted network.

### Core

- `GET /internal/info` - `{version, schema_version, timezone, revision, transports: [{name, type, public, preview}]}`.
- `POST /internal/config/validate` - body: complete user configuration (JSON). 200 `{"valid": true}` or 422 `{"valid": false, "errors": [...]}`. Nothing is applied. Zabbix is not contacted. Error texts contain no input values.
- `POST /internal/config/notify` - Core fetches the configuration from the web backend, validates and applies it. Returns `{changed, revision}`.
- `POST /internal/zabbix/identify` - body `{token}`. 200 `{userid, username, active, emails}`; 422 `{"error": "invalid_token"}`; 503 when Zabbix is unavailable; 502 for other Zabbix errors.
- `POST /internal/preview` - body `{user: {email, zabbix_token}, subscription: {...}}`. 200 `{content_type, subject, body}`. 400 `preview_not_supported`; 422 preparation error `{error, message, details}`; 503 Zabbix unavailable.
- `POST /internal/test-send` - same body. 200 `{"sent": true}`; 502 delivery failed.
- `POST /internal/send-now` - same body. Prepares and delivers the real digest. 200 `{result, shown, total, total_exact}` with `result` `delivered` or `empty` (nothing sent); 422 preparation error `{error, message, details}` (no error notification is sent); 503 Zabbix unavailable or shutdown; 502 delivery failed. The schedule state is not changed.

- `POST /internal/mail/login-link` - body `{email, url, ttl_minutes}`. Sends the login link email through the global `smtp` section of Core. 200 `{"sent": true}`; 502 `{error: "delivery_failed", message, details}` (details name the SMTP server and the cause). The URL contains the login token and is never logged.
- `POST /internal/mail/unsubscribed` - body `{email, title, manage_url}`. Sends the unsubscribe confirmation. Same answers.

When `internal_api_key` is not set, Core answers 404 on `/internal/*`.

### Web backend

- `GET /internal/config` - the authoritative user configuration as JSON.

## Configuration update sequence

- The web backend prepares the complete new configuration with `revision + 1`.
- It calls `POST /internal/config/validate` on Core. On 422 the change is rejected; on network errors the change is not saved and the user sees a "service unavailable" message.
- It checks that the revision on disk did not change meanwhile (409 otherwise) and writes the file atomically.
- It calls `POST /internal/config/notify`. Core calls `GET /internal/config`, validates again, applies the configuration and writes its cache.
- When the notification is lost, Core picks the change up at the next poll (`users.poll_interval`).

## Web user API (`/api/*`)

Used by the Vue application. Mutating requests require `X-Requested-With: zabbix-digest`. Responses use `Cache-Control: no-store`. Errors: `{error, message}`.

- `GET /api/public-config` - `{version, sentry_dsn, sentry_environment, help_url}` for the browser.
- `POST /api/auth/token` - `{token}` returns the eligible emails; `{token, email}` registers or logs in.
- `POST /api/auth/email` - `{email}`; sends a login link if the address is registered. Same answer for every address.
- `POST /api/auth/link` - `{token}` from the login link. 409 `token_required` when the stored Zabbix token is no longer valid.
- `POST /api/auth/logout`, `POST /api/auth/logout-all`.
- `GET /api/me` - profile, subscriptions, public transports, Core time zone.
- `POST /api/subscriptions`, `PUT /api/subscriptions/{id}`, `DELETE /api/subscriptions/{id}`.
- `POST /api/subscriptions/{id}/preview`, `POST /api/subscriptions/{id}/test-send`, `POST /api/subscriptions/{id}/send-now` (`{result, shown, total, total_exact}`).
- `POST /api/profile/token`, `GET /api/profile/emails`, `POST /api/profile/email`, `POST /api/profile/delete` (`{"confirm": "DELETE"}`).
- `POST /api/admin/impersonate` (`{email}`), `POST /api/admin/stop-impersonation`.
- `GET /api/unsubscribe/{token}`, `POST /api/unsubscribe/{token}` (no authentication).

Rate-limited endpoints return 429.
