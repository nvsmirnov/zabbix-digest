# Transports, adapters and the message contract

## Named transports

Transports are defined in the Core configuration under `transports`:

```yaml
transports:
  email:                 # name used in subscriptions; "email" is the default
    type: email          # adapter type: email | external
    public: true         # offered in the web UI
    send_timeout: 30s    # timeout of one delivery attempt
    retry: {attempts: 3, initial_delay: 10s, max_delay: 60s}
    settings: {}         # adapter settings
```

- Several transports may use the same adapter type, e.g. `email` and `corporate_email` with different SMTP servers.
- Non-public transports can only be set in `users.yaml` by an administrator.
- Delivery errors are retried with backoff. Permanent errors (SMTP authentication failure, rejected recipient, external exit code other than 0 and 75) are not retried.
- A delivery failure does not produce an error notification to the user. It is visible in logs and metrics.

## Email adapter

Settings (`transports.<name>.settings`):

- `from_address` - sender address. Default: `smtp.from_address`.
- `subject_prefix` - text added before every subject.
- `template_dir` - directory with custom templates. Missing files fall back to the built-in templates.
- `smtp` - a full SMTP section for this transport. Default: the global `smtp` section.

SMTP options (`smtp` or `settings.smtp`):

- `host`, `port`.
- `security`: `none` (plain), `starttls` (STARTTLS required, usually port 587), `tls` (implicit TLS, usually port 465).
- `username`, `password` - authentication is used when `username` is set.
- `verify_tls` (default true), `ca_file` - certificate verification.
- `timeout`, `from_address`, `local_hostname`.

Templates (Jinja2, HTML autoescaping on): `base.html.j2`, `_problems.html.j2`, `digest.html.j2`, `error.html.j2`, `test.html.j2`.
Built-in templates are in `src/core/src/core/transports/templates/`. Every template receives `m` (the message) and `subject`.
The email has an HTML part and a plain text part generated from the HTML. A `List-Unsubscribe` header is added when an unsubscribe URL exists.

## External adapter

```yaml
transports:
  custom_sender:
    type: external
    public: false
    send_timeout: 30s
    settings:
      command: ["/opt/sender/send.py", "--channel", "ops"]
      env: {SENDER_MODE: production}
      pass_environment: [PATH, LANG, LC_ALL, TZ]
      working_dir: null
      log_stderr: false
      log_stderr_limit: 500
```

- The program is started without a shell. Arguments come only from the administrator configuration.
- Standard input: the message as UTF-8 JSON (see below). Standard output is discarded.
- Environment: only variables listed in `pass_environment` plus `env`. Core secrets are not passed.
- Exit code 0: delivered. Exit code 75 (`EX_TEMPFAIL`): temporary failure, retried. Any other code: permanent failure.
- Timeout (`send_timeout`): the process is killed and the attempt counts as a temporary failure.
- Standard error is not logged unless `log_stderr: true`; then the first `log_stderr_limit` characters are logged, with known secrets masked.
- Preview is not supported for external adapters. Test send is supported.
- Example program: `examples/external-adapter/sample_sender.py`.

## Message contract (schema_version 1)

Example inputs: `examples/messages/digest.json`, `error.json`, `test.json`.

Common fields:

- `schema_version` - integer, currently 1. Independent of the application version.
- `type` - `digest`, `error` or `test`.
- `generated_at` - ISO 8601 time in the Core time zone.
- `timezone` - IANA name of the Core time zone.
- `recipient.email` - profile email.
- `subscription` - `id`, `name` (may be null), `transport`, `options` (adapter-specific `transport_options` of the subscription).
- `header`, `footer` - optional administrator texts (`messages.header`, `messages.footer`).
- `unsubscribe` - `url` (null without frontend) and `text` (administrator instruction).
- `links` - `widget` (source widget link), `zabbix` (Zabbix base URL).

`digest` (for `digest` and `test` messages):

- `widget_name`, `dashboard_name`.
- `problems` - list of problems in widget order. Each problem: `eventid`, `triggerid`, `name`, `severity` (`value` 0-5, `name`), `start_time` (ISO), `start_timestamp`, `duration_seconds`, `duration` (text), `acknowledged`, `suppressed`, `hosts` (list of `hostid`, `name`), `host_name`, `tags` (list of `tag`, `value`), `tags_display` (strings formatted as configured in the widget), `url` (event details).
- `shown` - number of problems in the list.
- `total` - number of matching problems.
- `total_exact` - false when more problems matched than Core fetched (`zabbix.max_problems`).
- `truncated` - true when not all matching problems are in the list.
- `display.show_tags` - show the tags column. Operational data is not part of the message (`opdata` and `display.show_opdata` were removed).
- `filters` - subscription filter overrides: `min_severity` (`value` 0-5, `name`, or null for the widget setting), `hide_acknowledged`, `hide_suppressed` (booleans). Added in schema_version 1 as an optional field; adapters must accept its absence.

`error` (for `error` messages):

- `category` - for example `zabbix_auth`, `zabbix_user_disabled`, `email_not_owned`, `widget_not_found`, `widget_unsupported`, `widget_url`, `zabbix_unavailable`, `zabbix_api`, `render`, `internal`.
- `message` - simple English explanation for the user.
- `details` - optional safe diagnostic text, may be null.

Messages never contain API tokens, passwords or internal keys. A `test` message contains synthetic problems whose names start with `TEST:`.

## Adding a built-in adapter

- Create a module in `src/core/src/core/transports/` with a class that implements:
  - `__init__(name, settings: dict, core_settings)` - validate settings; raise `ValueError` with a safe message.
  - `async send(message: dict)` - render and deliver; raise `TransportError` (retried), `PermanentTransportError` (not retried) or `RenderError`.
  - `supports_preview: bool` and `render_preview(message) -> {"content_type", "subject", "body"}`.
- Register the class in `adapter_types()` in `transports/base.py` and add the type name to `TransportConfig.type` in `core/config.py`.
- Add tests and documentation.
