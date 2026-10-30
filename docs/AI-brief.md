# Zabbix Digest Service — MVP Specification

## Purpose

Build a lightweight, self-hosted service that sends scheduled digests of active Zabbix problems to users.

Users subscribe to specific monitoring areas by configuring Problems widgets in Zabbix. The service reads widget settings, retrieves matching active problems, and delivers formatted reports through configurable transport adapters.

The primary transport is email, with HTML reports delivered directly in the message body.

The project must remain simple to deploy, configure, operate, maintain, and extend.

A web frontend provides optional self-service registration and subscription management.

The Core service must remain fully functional without the web frontend.

This document describes the requirements and architectural decisions for the initial MVP.

Implementation details may evolve as long as the agreed behavior is preserved.

---

## General Principles

- Keep the architecture simple.
- Avoid unnecessary dependencies and infrastructure components.
- Do not introduce a database unless a specific requirement cannot reasonably be implemented without one.
- Prefer human-readable configuration and state files.
- Prefer existing, maintained libraries over custom implementations.
- Separate Core processing, user management, and message delivery.
- Keep components independently deployable.
- Make optional features truly optional.
- Avoid storing redundant information.
- Avoid storing generated reports.
- Support Docker deployment without Docker-specific assumptions in application logic.
- Provide useful logs, metrics, and health checks.
- Treat user configuration as confidential information.
- Never expose API tokens or other secrets in logs, metrics, or error messages.
- Prefer predictable behavior over sophisticated automation.
- Do not introduce special-case logic when existing generic mechanisms can provide the required behavior.

### Language

All project artifacts must use simple technical English.

This includes:

- Source code and comments.
- UI text and error messages.
- Email messages and templates.
- Logs and metrics.
- Documentation.
- Configuration examples.
- API names and descriptions.
- Test names and descriptions.

Russian is used only for discussions during project planning and development.

Internationalization is outside the MVP scope.

---

## Architecture Overview

The project consists of two independently deployable components.

### Core

A Python service responsible for:

- Loading and validating configuration.
- Communicating with Zabbix API.
- Reading Problems widget settings.
- Retrieving and filtering active problems.
- Scheduling digest processing.
- Managing subscription execution state.
- Preparing structured message data.
- Invoking transport adapters.
- Handling retries and errors.
- Exposing HTTP endpoints for internal operations.
- Providing Prometheus metrics and health checks.

Core does not provide user-facing subscription management.

### Web Frontend

An optional application consisting of:

- Python FastAPI backend.
- Vue 3 frontend.
- Vite-based frontend build.

Responsibilities:

- User registration and authentication.
- User profile management.
- Subscription management.
- Configuration storage.
- Communication with Core.
- Preview, test-send and send-now operations (performed by Core).

The frontend does not contact Zabbix and does not send messages itself. Zabbix requests and all sending (digests, test messages, login links, unsubscribe confirmations) are done by Core; the frontend requests them through the internal API.
- Unsubscribe functionality.
- Limited administrator impersonation.
- Metrics and health checks.

The frontend backend serves the compiled Vue static files.

Node.js is required for building the frontend but not for running the production application.

### Component Independence

Core and Web Frontend run as separate processes and may run in separate containers.

Core supports two explicitly selected configuration sources:

- Local YAML file.
- HTTP API provided by the frontend backend.

The frontend is not required for normal Core operation when local file mode is selected.

When HTTP mode is selected, Core maintains a persistent local copy of the last successfully accepted user configuration.

Temporary frontend unavailability must not interrupt digest processing when a valid cached configuration exists.

---

## Zabbix Integration

### Supported Versions

Primary target:

- Zabbix 7.0 LTS.

Other Zabbix 7.x versions may work but are not guaranteed until tested.

Core must not reject an instance solely because its reported Zabbix version differs from the primary target.

No mandatory Zabbix version check is performed during startup.

Compatibility problems are handled as regular API or report preparation errors.

Zabbix-specific behavior should be isolated in the integration layer.

### Zabbix Instance

The MVP supports one Zabbix instance.

Its URL is configured globally in Core.

Multiple Zabbix instances are outside the MVP scope.

### Authentication

Every user creates their own Zabbix API token.

The token is stored in the user configuration.

Zabbix API calls for a user must use that user's token.

Core must not use a shared privileged Zabbix token to retrieve problems on behalf of users.

Zabbix permissions must remain the source of authorization for accessing monitoring data.

### Token and User Validation

Validate the user's Zabbix identity:

- During initial registration.
- During authentication with a new API token.
- During subsequent authentication when an existing token is used.
- Before scheduled digest processing.

Validation must establish:

- The API token is valid.
- The corresponding Zabbix user is active.
- The configured email belongs to the email addresses configured for that Zabbix user.

The exact API methods and handling of Zabbix media settings must be verified against the target Zabbix version.

A user with multiple email addresses may select any eligible address returned from Zabbix.

A token belonging to a different user must not be accepted for an existing profile merely because it can access the same monitoring data.

### Invalid User Configuration

The following conditions are treated as user configuration problems:

- Invalid or revoked API token.
- Disabled Zabbix user.
- Configured email no longer belongs to the Zabbix user.
- Inaccessible or removed Problems widget.
- Other persistent errors preventing report preparation.

When the user's identity or access becomes invalid:

- Preserve the user profile.
- Preserve the subscription settings.
- Do not change the subscription's `enabled` value automatically.
- Suspend normal digest delivery while the configuration remains invalid.
- Send the regular preparation-error notification at the scheduled processing time.
- Resume digest delivery automatically after the problem is corrected.

Error notifications may be repeated daily.

This is intentional: persistent problems should continue generating reminders.

No persistent error-notification suppression mechanism is required.

### Temporary Zabbix Errors

Temporary errors must be distinguished from confirmed invalid configuration.

Examples:

- Connection timeout.
- Temporary DNS failure.
- HTTP service unavailable.
- Temporary server-side failure.

Such errors must not permanently invalidate the user's configuration.

Use retry with backoff.

After retries are exhausted, report preparation fails for the current execution.

Send an error notification when possible.

Future scheduled executions continue normally.

Temporary Zabbix unavailability must not automatically revoke existing web sessions.

---

## Problems Widget as the Filter Source

### General Behavior

Each subscription references exactly one Zabbix Problems widget.

The user configures filtering directly in Zabbix.

The subscription stores the original Zabbix URL of the dashboard that contains the widget.

Core must extract the required identifiers from that URL.

A copied Zabbix 7.0 dashboard URL identifies the dashboard only, not a single widget.

Core uses the first Problems widget of the dashboard:

- The first dashboard page that contains a Problems widget.
- On that page, the top-left Problems widget (smallest vertical position, then smallest horizontal position).

A dashboard without a Problems widget produces a clear preparation error.

Unsupported links must produce a clear configuration or preparation error.

### Reading Widget Configuration

Core retrieves the current widget settings through Zabbix API whenever a digest is prepared.

Widget settings are not persistently cached.

Changes made to a Problems widget in Zabbix automatically affect subsequent digest generation.

Core interprets the widget settings and retrieves matching active problems through Zabbix API.

The implementation must aim to reproduce the effective Problems widget filtering behavior.

### Filtering

Filtering must follow the supported settings of the source Problems widget, including relevant options such as:

- Host groups.
- Hosts.
- Severity.
- Tags.
- Acknowledgement filters.
- Suppression filters.
- Other supported Problems widget filters.

Do not add separate per-subscription filters in the MVP, except the filter overrides below.

Acknowledged and suppressed problems must be handled according to the widget settings.

The exact correspondence between widget parameters and Zabbix API filters must be verified during implementation.

Unsupported filtering behavior must not silently produce misleading results.

### Filter Overrides

A subscription may set optional filter overrides. By default each override keeps the widget setting.

- Minimal severity (`min_severity`): a Zabbix severity 0-5 or its name.
- Hide acknowledged problems (`hide_acknowledged`).
- Hide suppressed problems (`hide_suppressed`).

Overrides only narrow the widget filter and never include problems the widget filter excludes:

- Minimal severity: the effective severities are the widget severities that are equal to or higher than the minimal severity. A widget without selected severities means all severities.
- Hide acknowledged: a widget that shows all problems shows unacknowledged problems only. A widget that shows acknowledged problems only gives an empty report.
- Hide suppressed: suppressed problems are excluded even when the widget shows them.
- When no problem can match, the report is empty and handled as an empty report.

The digest states the active overrides.

The web frontend lets the user set each override or keep the widget setting.

### Active Problems Only

Digests contain only problems active at the time of report preparation.

Resolved problems and general event history are excluded.

The service does not generate summaries of all events occurring during the previous 24 hours.

### Empty Reports

When no active problems match the filters:

- Do not send a digest.
- Treat the scheduled execution as successfully completed.
- Update the subscription execution state accordingly.
- Reset consecutive processing failures.

### Result Limits

Use the problem display limit configured in the Problems widget.

The relevant Zabbix widget setting is `show_lines`.

If additional matching problems exist beyond the displayed limit:

- Show a prominent warning.
- Prefer displaying the number of included and total matching problems.
- If the exact total cannot be determined reliably, indicate that the report is incomplete.
- Provide a link to the original Problems widget.

Do not silently truncate reports.

### Sorting

Use sorting configured in the Problems widget.

If the required sorting cannot be reproduced directly through the Zabbix API, implement the corresponding sorting in Core.

The resulting order should match the widget behavior as closely as practical.

### Data Consistency

A digest represents a snapshot of Zabbix data collected during report preparation.

Do not revalidate individual problems immediately before sending the report.

Include the report generation timestamp.

If an execution is retried after an error, retrieve fresh data and generate the report again.

Previously generated reports are not stored.

---

## Digest Content

### General Format

Core produces structured message data.

Transport adapters are responsible for rendering and delivering messages.

Core must not generate email-specific HTML.

The default email adapter renders an HTML report directly in the email body.

PDF generation is not required.

### Default Columns

The HTML digest contains these primary columns:

- Time.
- Severity.
- Host.
- Problem.
- Duration.

Additional information follows the Problems widget settings:

- Tags.

Operational data is not shown: the widget setting is ignored (see `docs/decisions.md`).

Timeline visualization is not reproduced in email.

The HTML digest does not attempt to reproduce the entire Zabbix UI.

### Links

Every digest includes a link to its source Problems widget.

Each problem name is a clickable link to the corresponding Zabbix Event details page.

Host names do not require separate links.

Links must be constructed using identifiers obtained from Zabbix rather than guessed values.

### Digest Title

Subscriptions may have an optional `name`.

If present, use it in the report subject. Error notifications also show it in the content. A digest does not repeat it in the content (see Layout).

If absent, do not display a generated subscription title in the rendered report.

The subscription ID remains available for internal identification and user interface display.

### Layout

The problem table comes first in a digest, so mail clients and mobile notifications that show the start of a message show problem data.

After the table:

- The truncation warning, if any.
- `Total N active problem(s)`.
- `Generated at <time> (<time zone>)`.
- `Source: widget "<widget name>" on dashboard "<dashboard name>"`, linked to the dashboard.
- Active subscription filter overrides.

The plain text part follows the same order, one problem per line, without the table header row.

A test message has the same layout, with the TEST banner above the table.

### Header and Footer

The structured message may contain configurable header and footer text.

Every email includes a small footer with instructions for unsubscribing.

When the web frontend is available, the footer includes the subscription-specific unsubscribe link.

Without the frontend, the footer uses an administrator-provided unsubscribe instruction from Core configuration.

For example, it may contain a service desk procedure or administrator contact information.

The footer must also be present in error and test messages when applicable.

---

## Subscriptions

### Subscription Model

A user may have multiple subscriptions.

Each subscription:

- References exactly one Problems widget.
- Has its own schedule.
- Uses one configured transport.
- Has a globally unique ID.
- May have an optional name.
- Can be enabled or disabled.
- May have optional filter overrides (see Filter Overrides).
- Has an independent unsubscribe token when the web frontend is used.

One subscription produces one digest.

Multiple subscriptions are not combined into one email.

### Subscription ID

IDs are generated as random hexadecimal strings.

Default length:

- Four hexadecimal characters.

Allowed characters:

- `0-9`.
- `a-f`.

The ID length is configurable.

Changing the configured length affects only newly created subscriptions.

Existing IDs must never be regenerated merely because the configured ID length changes.

Uniqueness is required across the entire user configuration, regardless of ID length.

On collision, generate another ID.

If no unique ID can be generated within the configured identifier space, subscription creation fails.

Deleted IDs may be reused.

### Subscription Naming in UI

Display:

- `Subscription name (#id)` when a name exists.
- `#id` when no name exists.

The name is editable.

The ID is not editable.

### Subscription Limits

The frontend supports a configurable maximum number of subscriptions per user.

Example setting:

`max_subscriptions_per_user`

The limit may be disabled.

Reducing the limit must not delete existing subscriptions.

New subscription creation is rejected when the applicable limit is reached.

### Disabling Subscriptions

Users may temporarily disable subscriptions.

Disabled subscriptions remain in the configuration.

They can be enabled again without reconfiguration.

Disabled subscriptions are not scheduled for digest delivery.

### Deleting Subscriptions

Users may permanently delete subscriptions.

Deletion requires confirmation in the UI.

Deleted subscriptions disappear from the user configuration.

Their unsubscribe tokens become invalid.

Core eventually removes associated state files.

---

## Scheduling

### Execution Model

Core is a long-running Python process with an internal scheduler.

The scheduler periodically checks which subscriptions are due.

A one-shot execution mode is also provided for testing and troubleshooting.

The one-shot mode has a forced option:

- It sends digests of enabled subscriptions immediately, even if their scheduled time has not come.
- It uses the regular report processing and delivery.
- It does not change the scheduled execution state.

No external cron service is required for normal operation.

### Time Zones

All scheduled times use the local time zone configured for the Core process.

Per-user and per-subscription time zones are not supported.

The frontend must display the Core time zone near schedule configuration controls.

Daylight-saving behavior follows the service's configured local time zone.

The exact behavior for ambiguous or nonexistent local times during DST transitions must be defined and tested during implementation.

### Daily Schedule

Every subscription has an individual daily execution time.

The intended behavior is no more than one completed scheduled execution per subscription per local calendar day, except after a time change (below).

If a subscription's time is changed after that day's execution has completed, and the new time has not yet passed today, the digest is sent again today at the new time. If the new time has already passed today, no further digest is sent that day.

If no execution has occurred yet, the updated schedule may apply immediately.

### Concurrency

Subscriptions are processed concurrently with a global limit.

Example:

`max_workers: 5`

The value is configured in Core.

Errors in one subscription must not block unrelated subscriptions.

A single subscription must not be processed concurrently by multiple workers within the same Core process.

The MVP assumes one active Core scheduler instance.

Distributed execution and cross-process locking are outside scope.

### Zabbix API Concurrency

Provide a separate global limit for simultaneous Zabbix API requests.

Example:

`zabbix_max_concurrent_requests: 10`

The limit applies to:

- Digest processing.
- Token validation.
- Preview.
- Retries.
- Other Zabbix API operations performed by Core.

No request-per-second rate limiter is required for the Zabbix API in the MVP.

### Configuration Changes During Execution

Each running task uses a configuration snapshot taken when that task started.

Configuration changes do not cancel already running tasks.

This applies to:

- Editing subscriptions.
- Disabling subscriptions.
- Deleting subscriptions.
- Deleting user profiles.

New tasks use the latest accepted configuration.

A task may complete delivery after its subscription or user profile has been removed.

This behavior is accepted.

---

## Execution State

### Storage

Use human-readable YAML files.

Do not use SQLite or another database.

Store one state file per subscription.

Example:

`state/<subscription_id>.yaml`

Subscription IDs are globally unique, so email addresses are not required in file paths.

### Stored Information

Store only current execution state, not an event history.

Relevant fields include:

- Last successful execution.
- Last failed execution.
- Number of consecutive failures.
- Last completed scheduled slot.
- Information identifying a delivery attempt in progress, if needed.

The final schema must distinguish successful digest delivery, successful empty execution, and failed execution.

No historical list of every attempt is required.

### State Updates

Write state files atomically.

Avoid partially written YAML files.

Only one worker within a Core process may update a subscription state at a time.

A running task may finish writing its state even when its subscription has been deleted from the latest configuration.

The cleanup mechanism handles obsolete files.

### Completed Executions

A scheduled execution is successfully completed when:

- A digest was delivered successfully.
- No matching active problems were found.

A successful preparation-error notification does not mean the digest itself was successful.

In this case:

- Record the failure.
- Increment consecutive failures.
- Mark the scheduled execution as completed to avoid duplicate error notifications for the same slot.

### Failed Executions

A failed delivery or preparation attempt is recorded in the state file.

Failure counters are updated accordingly.

After a subsequent successful scheduled execution, consecutive failures are reset.

### Recovery

After restart, Core uses the saved state to determine which scheduled executions still require processing.

If a delivery was started but not recorded as successfully completed, treat it as not delivered.

No special recovery branch is required for this case.

The normal scheduling and retry rules apply.

Rare duplicate delivery after an abnormal process termination is acceptable.

### Delivery Window

Provide a global configurable `delivery_window`.

Example:

`delivery_window: 2h`

The same window applies to:

- Recovering scheduled executions after restart.
- Retrying failed scheduled executions.
- Handling delays caused by temporary service unavailability.

When the window expires, the pending execution is no longer retried.

Do not carry outdated executions indefinitely into later days.

The scheduler must handle midnight and date boundaries consistently.

### State Cleanup

When a subscription is removed, Core removes its state file.

Additionally, perform periodic cleanup to remove orphaned state files.

The cleanup interval is configurable.

Cleanup must operate against the latest successfully accepted user configuration.

Only recognized subscription state files may be automatically deleted.

A state file recreated by an already running task after subscription deletion may remain until the next cleanup.

---

## Graceful Shutdown

### General Behavior

Core must support graceful shutdown on termination signals.

When shutdown begins:

- Stop scheduling new tasks.
- Do not start new subscription processing.
- Allow active transport delivery operations to finish.
- Persist completed delivery results.
- Stop the process after active operations finish or the shutdown deadline is reached.

Report preparation may be interrupted and repeated after restart.

### Transport Delivery

If a transport delivery operation has already started, Core should make a reasonable effort to let it finish.

Examples include:

- An active SMTP transaction.
- An already launched external sender process.

Each transport has a configurable delivery timeout.

Example:

`send_timeout: 30s`

A global shutdown timeout must provide sufficient time for normal active deliveries to finish.

No operation may block shutdown indefinitely.

### Delivery Guarantees

The system should minimize missed and duplicate messages.

Absolute exactly-once delivery is not guaranteed.

For example, an SMTP server may accept a message immediately before the application terminates without persisting the success state.

In such a case, retry may create a duplicate.

This residual risk is accepted.

Avoid introducing distributed transactions or complex delivery journals solely to eliminate it.

---

## Retry and Error Handling

### General Principles

Distinguish:

- Report preparation errors.
- Transport delivery errors.
- Temporary external errors.
- Permanent configuration errors.
- Unexpected application errors.

### Zabbix API Retries

Retry individual API requests when an error is considered temporary.

Use configurable retry counts and backoff.

Do not retry the entire digest pipeline merely because one API request failed.

Confirmed invalid credentials and other permanent errors should not trigger unnecessary retries.

### Transport Retries

Transport adapters support retry with backoff.

Retry settings are configured in Core.

Each transport also has its own operation timeout.

The adapter reports success or failure to Core.

No fallback transport is required in the MVP.

### Scheduled Retry Behavior

After a failed scheduled attempt, Core may retry within the configured delivery window.

Each new scheduled retry attempt prepares a fresh report from current Zabbix data.

Do not save previously rendered HTML or problem lists for later retries.

If matching problems disappear before a retry, the execution may complete successfully without sending a digest.

### Preparation Errors

Examples:

- Zabbix token invalid.
- Zabbix user disabled.
- Email no longer associated with the Zabbix user.
- Widget missing.
- Widget inaccessible.
- Zabbix temporarily unavailable after retries.
- Unsupported widget settings.
- Report rendering failure.

After exhausting applicable retries, generate an error notification.

Deliver the error through the subscription's configured transport when possible.

The error message must explain the failure in simple English.

Include the regular unsubscribe instructions.

The same persistent problem may produce an error notification every scheduled day.

### Delivery Errors

Examples:

- SMTP server unavailable.
- SMTP authentication failed.
- External sender failed.
- Transport timeout.

If report preparation succeeded but message delivery failed:

- Apply delivery retries.
- Record the failure.
- Update metrics.
- Write logs.
- Do not send a separate error notification to the user through the same transport.

Delivery failures are the responsibility of service monitoring and administration.

### Expected vs Unexpected Errors

Expected operational failures must be handled explicitly.

Unexpected failures indicate possible application defects.

Unexpected errors must be logged and reported to Sentry when configured.

Never expose secrets in user-facing error messages.

---

## Transport Architecture

### General Model

Core produces a structured JSON-compatible message.

Transport adapters perform:

- Rendering.
- Recipient-specific formatting.
- Delivery.

Core must not depend on transport-specific output formats.

### Built-in Adapters

Built-in adapters are Python modules implementing a common interface.

The MVP includes:

- Built-in email adapter.

The architecture must permit adding other built-in adapters later.

Potential future adapters include Telegram and other messaging systems.

A Telegram adapter is not required in the MVP.

### External Adapter

Provide a generic external program adapter.

Core passes the structured message as JSON through standard input.

The external program is responsible for rendering and delivery.

The invocation mechanism must define:

- Command configuration.
- Standard input format.
- Exit code interpretation.
- Timeout.
- Error handling.
- Logging behavior.

External program output must not be blindly included in logs if it may contain secrets.

Avoid shell interpretation of arbitrary user-controlled values.

External sender commands are configured by the service administrator, not by ordinary users.

### Named Transport Instances

The administrator defines named transport instances in Core configuration.

Each instance specifies:

- Name.
- Adapter type.
- Adapter settings.
- Transport-specific timeout.
- Visibility in the frontend.

Multiple instances may use the same adapter type.

Example conceptual names:

- `email`.
- `corporate_email`.
- `telegram_ops`.
- `custom_sender`.

The default transport is `email`.

### Transport Visibility

Each transport instance supports a `public` setting.

Public transports are available for selection in the frontend.

Non-public transports may be assigned through manual user configuration.

Transport credentials and connection settings must never be exposed to ordinary users.

### Transport Selection

Each subscription selects exactly one named transport.

Multiple simultaneous transports per subscription are outside the MVP scope.

Transport-specific recipient settings may be stored in the subscription when required by the adapter.

For email, the destination address is always the user's profile email.

---

## Structured Message Contract

### General Requirements

Use one common JSON contract for all message types.

Include a `schema_version` field.

Supported message types:

- `digest`.
- `error`.
- `test`.

The common data model should include:

- Schema version.
- Message type.
- Recipient.
- Subscription information.
- Generation timestamp.
- Header text.
- Footer text.
- Relevant links.
- Message-specific payload.

Digest messages contain structured problem data.

Error messages contain structured error information.

Test messages contain synthetic test problem data.

### Structured Problem Data

Provide sufficient information for adapters to render meaningful reports.

Relevant fields may include:

- Event identifier.
- Trigger identifier.
- Host information.
- Problem description.
- Severity.
- Start time.
- Duration.
- Acknowledgement information.
- Tags.
- Event details URL.

Include information about truncation when the widget result limit is exceeded.

Avoid passing secrets or unrelated Zabbix API response fields to transport adapters.

### Error Data

Provide:

- Error category.
- Safe error message.
- Optional safe diagnostic details.
- Relevant subscription information.

Do not include API tokens, passwords, or other secrets.

### Contract Versioning

The JSON contract is versioned independently of the application version.

Changes must preserve compatibility where practical.

External adapters must have a documented schema and example inputs.

---

## Email Adapter

### Delivery

The email adapter is built into the project.

SMTP configuration is defined in Core configuration. The frontend has no SMTP settings.

Support standard SMTP authentication and secure connection options.

The exact SMTP connection and TLS options must be documented.

Transport timeouts and retries are configurable.

### Rendering

The adapter renders HTML emails.

Digest reports appear directly in the email body.

No PDF attachment is generated.

The message subject reflects its type and optional subscription name.

### Templates

Provide built-in Jinja2 templates for:

- Digest messages.
- Error notifications.
- Test messages.

Templates may be overridden through a configured custom template directory.

If a custom template is absent, use the corresponding built-in template.

The templates receive structured message data.

Templates must safely escape untrusted values from Zabbix and user configuration.

### Message Footer

Include unsubscribe instructions in every applicable message.

Use visually unobtrusive formatting.

The instructions must remain readable.

---

## Preview and Test Sending

### General

Support both CLI and Web UI operations.

Preview and test sending are separate features.

They must reuse the regular report processing and adapter mechanisms wherever possible.

### Preview

Preview uses:

- The actual subscription.
- Current Problems widget settings.
- Real Zabbix problem data.
- The selected built-in adapter's renderer.

Preview must not deliver any message.

For email, display rendered HTML in the browser.

Preview does not modify the scheduled execution state.

Preview is supported only by built-in adapters that implement this capability.

External adapters do not support Preview in the MVP.

The frontend disables Preview when the selected adapter does not support it.

### Test Send

Test sending uses synthetic problem data.

It must not depend on the existence of real active problems.

The test message must contain prominent TEST markings in:

- The subject.
- The message body.

Test sending performs actual delivery through the selected transport.

It is available for built-in and external adapters.

Test sending does not modify the regular scheduling state.

It must use the regular adapter configuration.

### Send Now

Send now prepares and delivers the real digest of a subscription immediately, outside its schedule.

- It uses the regular report processing, real Zabbix data and the selected transport.
- It is available for built-in and external adapters.
- An empty report is not sent; the user is told that there were no active problems.
- Preparation errors are shown to the user; no error notification is sent.
- It does not modify the regular scheduling state; the scheduled digest of the day is still sent.

The CLI equivalent is the forced one-shot mode (`once --force`).

### Access

Provide equivalent administrative testing functionality through CLI.

Provide user-facing Preview, Test Send and Send Now controls in the frontend.

Users may test only their own subscriptions, except during authorized impersonation.

---

## User Configuration

### Storage

All users and subscriptions are stored in one YAML document.

Example filename:

`users.yaml`

The format must remain easy to create and edit manually.

Provide:

`users.sample.yaml`

The example must be complete enough for running Core without the frontend.

Avoid unnecessary nesting and redundant fields.

### User Identity

The user's email is the unique configuration key and delivery address.

Each user has:

- Email.
- Zabbix API token.
- Subscriptions.
- Session-related metadata when web authentication is enabled.

Additional metadata may be stored when necessary.

### Confidentiality

The entire user configuration is considered secret.

Protect file permissions accordingly.

Do not log the configuration contents.

Do not expose the complete configuration through public APIs.

The Core-to-Frontend configuration API requires authentication.

### Configuration Revision

The user configuration contains a `revision` field.

The frontend increments the revision on successful changes.

Core tracks the currently applied revision.

Revision values support conflict detection.

Do not maintain a complete history of revisions.

Checksums are not required.

### Manual Configuration

In file mode, the administrator maintains the user configuration manually.

The format must remain understandable without special editing tools.

Manual changes must not require a frontend.

### Moving from File Mode to HTTP Mode

An administrator may manually migrate the user YAML configuration into the frontend backend's storage.

No migration UI or automated import command is required in the MVP.

Provide documentation describing the procedure.

---

## Core Configuration

### General Requirements

Core has its own configuration, separate from user configuration.

It contains settings such as:

- Zabbix URL.
- User configuration source.
- Frontend connection settings.
- Shared internal API key.
- Scheduler settings.
- Retry settings.
- Delivery window.
- Concurrency limits.
- Transport definitions.
- SMTP settings.
- Logging configuration.
- Metrics and HTTP settings.
- Optional Sentry DSN.
- State directory location.

### YAML and Environment Variables

Support YAML configuration with automatic environment variable overrides.

Precedence:

- Environment variables.
- YAML values.
- Application defaults.

The application should also support configuration entirely through environment variables.

Use an automatic and documented mapping between nested YAML keys and environment variable names.

Example:

`SMTP__HOST` corresponds to `smtp.host`.

Nested settings and basic data types must be handled consistently.

Use a maintained configuration library where practical.

Pydantic Settings is a preferred candidate, subject to compatibility with the chosen Python version.

### Validation

Validate the effective configuration after merging all sources.

Unknown or invalid configuration values should produce clear errors.

The Core configuration is loaded at startup.

Changes require a process restart.

Automatic hot reload is not required.

### Frontend Configuration

The frontend backend follows the same YAML/ENV configuration principles.

Its settings include:

- Shared internal API key.
- User configuration storage path.
- Core API URL.
- Session signing secret.
- Administrator email list.
- Rate limits.
- Subscription limits.
- Logging.
- Metrics.
- Optional Sentry DSN values.
- Other web-specific settings.

---

## User Configuration Sources

### Explicit Mode Selection

Core supports:

- `file`.
- `http`.

The mode is explicitly configured.

Do not automatically select a source based on availability.

### File Mode

During startup:

- Require the configured user YAML file.
- Load the entire configuration.
- Validate it.
- Exit with a nonzero status if the file is missing or invalid.

Normal operation uses the local file.

The file is read only at startup. Changes to the file take effect after a Core restart.

### HTTP Mode

The frontend backend is the primary source.

Core maintains a persistent local cached copy of the last valid configuration.

At startup:

- Attempt to contact the frontend backend.
- Use retries and backoff.
- Do not start normal scheduling until startup synchronization succeeds or the configured startup retry sequence finishes.
- If synchronization fails and a valid cached configuration exists, start normal operation using the cache.
- If synchronization fails and no valid cache exists, continue retrying indefinitely with reasonable backoff.
- Do not start digest scheduling until a valid configuration becomes available.

After startup:

- Continue periodic polling.
- Accept authenticated update notifications.
- Keep using the last valid configuration when the frontend is unavailable.

Frontend unavailability must not stop normal digest processing when valid cached configuration exists.

### Local Cache

Persist the last accepted user configuration to disk.

The cached file is secret.

Use atomic writes.

Validate configuration before replacing the current cache.

Never replace a valid cached configuration with an invalid one.

The cache must remain usable after Core restarts.

---

## Core and Frontend Synchronization

### General Model

Use both:

- Periodic polling.
- Push notifications on configuration changes.

The frontend backend stores the authoritative user configuration in HTTP mode.

Core retains a local persistent copy.

### Authentication

Use one shared API key for internal API requests in both directions.

Send the key in an HTTP header.

The key must not be exposed to browsers.

Internal API endpoints must not be publicly accessible.

Use HTTPS when traffic crosses an untrusted network.

### Internal API

Reserve the `/internal/*` namespace for authenticated component-to-component operations.

Use `/api/*` for frontend user operations.

Health and metrics endpoints remain separate.

### Configuration Update Sequence

When a user modifies configuration in the frontend:

- Frontend prepares the complete updated configuration in memory.
- Frontend sends it to Core for validation.
- Core validates the entire configuration without applying it.
- If validation fails, frontend rejects the change.
- If validation succeeds, frontend atomically saves the new YAML revision.
- Frontend notifies Core that an update is available.
- Core fetches the new configuration.
- Core validates the complete configuration again.
- Core applies it and persists its local cache.

The configuration must not be partially applied.

### Validation Ownership

Core is the only component responsible for complete user configuration validation.

The frontend must not implement a second independent copy of the same validation rules.

Basic UI input constraints are permitted for usability, but they are not authoritative validation.

### Validation Scope

Configuration validation includes:

- YAML structure.
- Required fields.
- Field types.
- Valid identifiers.
- Unique subscription IDs.
- Valid revision metadata.
- Recognized transports.
- Supported configuration values.
- Syntactically valid URLs.

Do not contact Zabbix while validating the entire user configuration.

Zabbix access, widget existence, token validity, and user identity are checked separately during their respective operations.

### Revision Conflicts

Use the configuration revision to detect concurrent modifications.

If another operation has already changed the stored revision, reject the stale update.

The user must be able to retry without losing entered data.

### Core Unavailability

If Core cannot validate a proposed configuration:

- Do not save the change.
- Keep the user on the same form.
- Preserve entered values.
- Display a useful error.
- Include a suggestion to contact the administrator.
- Allow the user to press Save again.

Core unavailability is considered a service-level operational failure.

Existing Core processing continues independently if it has valid configuration.

### Validation Failure Reporting

Record validation failures in logs and Prometheus metrics.

Do not expose secrets or internal exception traces to the user.

---

## Web Frontend

### Technology

Use:

- FastAPI for the backend.
- Vue 3 for the frontend.
- Vite for building frontend assets.

Serve the compiled frontend from FastAPI.

Do not require a separate Node.js runtime in production.

### User Interface

The frontend provides:

- Initial registration.
- Login.
- Subscription list.
- Subscription creation.
- Subscription editing.
- Subscription enable/disable.
- Subscription deletion.
- Preview.
- Test send.
- Send now.
- API token replacement.
- Email selection and change.
- Logout.
- Logout from all sessions.
- Profile deletion.
- Unsubscribe confirmation.
- Administrator impersonation control.

No full administrative management interface is required.

The page header shows the email of the signed-in user (the target user during impersonation).

An optional help URL (`help_url`) adds a Help link right of the navigation buttons and at the bottom of the login page. It looks like the other buttons but is a real link, so the browser can open it in a new tab or copy its URL. Without the setting no link is shown.

The subscription form asks for a dashboard link and states that the first Problems widget of the dashboard is used.

The Docker Compose example uses a test SMTP server with a web mailbox. It logs every received message.

### User Experience

Forms must preserve user-entered values when saving fails.

Errors should use simple English.

Subscription titles in the UI follow the format:

- `Name (#id)`.
- `#id`.

Show the Core time zone near schedule selection.

Only published transport instances are available in transport selection controls.

Do not expose transport credentials or stored Zabbix API tokens.

---

## User Registration

### Initial Authentication

The first login requires a user-generated Zabbix API token.

The backend validates the token against Zabbix.

After successful validation:

- Identify the Zabbix user.
- Retrieve eligible email addresses.
- Let the user choose an email.
- Create the profile.
- Store the API token.
- Create the authenticated session.

The email becomes the profile identifier.

### Existing Email

If the selected email is already registered, do not create another profile with the same identity.

Do not automatically merge user profiles.

### Zabbix Identity

The frontend must not trust arbitrary email addresses supplied by users during registration.

Only email addresses verified as belonging to the authenticated Zabbix user may be selected.

---

## Web Authentication

### General Model

Use hybrid authentication.

Initial registration requires a Zabbix API token.

Subsequent authentication may use a one-time email link.

No separate password database is required.

### Email Login

The user enters an email address.

If the email belongs to a previously verified registered profile, the frontend may request a one-time login link. Core sends it.

Before completing a new login, validate the saved Zabbix API token and user status.

If the token is invalid, require a replacement token.

If Zabbix is temporarily unavailable, fail only the current authentication attempt.

Do not classify a temporary API failure as confirmed invalid credentials.

### One-Time Links

One-time login links must:

- Be cryptographically unpredictable.
- Have a short validity period.
- Be invalid after successful use.
- Be protected against replay.
- Not expose private user information.

The implementation must support one-time use without requiring a full database.

### Spam Protection

Do not send login messages to arbitrary unregistered email addresses.

Use rate limiting for login-link requests.

Do not reveal whether an email is registered through publicly visible responses.

Limit abuse using both IP-based and email-based controls.

### Remembered Email

The browser may remember the last entered email address locally.

Prepopulate the login form when available.

Do not store the Zabbix API token in browser-local persistent storage.

---

## Web Sessions

### Session Storage

Use signed cookies.

Do not create a separate server-side session database.

Regular sessions may be effectively long-lived.

A browser cookie may remain valid until explicitly revoked, subject to signing-key validity and account state.

Use appropriate cookie security settings:

- HttpOnly.
- Secure in HTTPS deployments.
- Suitable SameSite policy.

### Session Version

Store `session_version` in each user profile.

Include the corresponding value in the signed session.

Validate the version against the current profile on authenticated requests.

Changing the version invalidates previously issued sessions.

### Profile Generation Identity

Each registered profile has an immutable, unique generation identifier.

The identifier changes if the profile is deleted and later recreated.

This prevents cookies from a previous registration from becoming valid for a new profile using the same email.

### Logout

Normal logout clears the current browser's authentication cookie.

### Logout All Sessions

Provide a **Log out all sessions** action.

This operation increments or changes the profile's session version.

All earlier sessions become invalid.

Document how an administrator can perform the same operation through manual configuration changes.

### Existing Sessions During Zabbix Outage

An already authenticated user may continue to:

- View subscriptions.
- Create subscriptions.
- Edit subscriptions.
- Disable subscriptions.
- Delete subscriptions.
- Delete their profile.

Operations requiring fresh Zabbix verification remain unavailable during a Zabbix outage.

These include:

- Changing API token.
- Changing profile email.

Preview requires Zabbix access and may fail while Zabbix is unavailable.

Existing sessions are not revoked solely due to temporary Zabbix unavailability.

---

## Profile Management

### API Token Replacement

The stored Zabbix token must never be returned to the browser.

The UI may indicate that a token is configured.

Users may replace the token.

Before saving a replacement:

- Validate the new token.
- Verify the Zabbix user identity.
- Verify the configured email belongs to that Zabbix user.

If validation fails, preserve the existing token.

### Email Changes

Users may change their profile email only to another eligible address configured for the same Zabbix user.

The frontend must validate the change through Zabbix.

If the target email is already registered in the service, reject the change.

Do not merge profiles.

When the email changes:

- Preserve all subscriptions.
- Preserve subscription IDs.
- Preserve unsubscribe tokens.
- Preserve execution state.
- Change the delivery address.

Authentication sessions must remain associated with the same profile generation, not with a mutable email string.

### Profile Deletion

Users may delete their profile.

Require explicit UI confirmation, such as entering `DELETE`.

No additional email confirmation is required.

Deletion removes:

- User profile.
- All subscriptions.
- User session metadata.

Existing sessions become invalid.

Unsubscribe links become invalid.

Core cleans up obsolete subscription state files.

Already running digest tasks may finish according to the general configuration snapshot rules.

---

## Administrator Impersonation

### Purpose

Provide a minimal support feature allowing designated administrators to act as a user.

Do not create a full administrative interface.

### Administrator Configuration

Administrators are defined by email in frontend backend configuration.

Example:

`admin_emails`

Changing the administrator list requires restarting the frontend backend.

Administrative privileges are not stored in `users.yaml`.

### UI

Authorized administrators see an **Impersonate** button.

The button allows entering a target user's email.

After impersonation starts, show the normal user UI.

The only required additional UI control is:

**Stop impersonation**

Do not introduce additional administrative screens.

### Permissions

Impersonation grants the same application capabilities as the target user.

Do not add special restrictions or privileges inside the impersonated profile.

Operations use the target user's normal permissions and configuration.

### Session Behavior

Use a separate impersonation session cookie.

The cookie must not be persistent across browser sessions.

Also apply a server-enforced expiration time, for example one hour.

A browser session cookie alone is not sufficient to guarantee expiration when a browser window closes.

The original administrator session must remain available for returning from impersonation.

### Revocation

The impersonation session is subject to the target user's `session_version`.

If the target user invalidates all sessions, impersonation also becomes invalid.

The originating administrator session must also remain valid.

If the target profile is deleted, the impersonation session becomes invalid through the normal session validation mechanism.

Avoid special-case account-deletion logic for impersonation.

### Email Changes

Changing the target user's email does not end impersonation.

The session remains associated with the target profile generation.

### Logging

Log impersonation activity with:

- Original administrator identity.
- Target user identity.
- Action performed.

Do not treat the impersonated user as the true actor in audit logs.

---

## Unsubscribe

### General Behavior

Every web-managed subscription has its own unsubscribe token.

The token is:

- Cryptographically random.
- Independent of the subscription ID.
- Generated once.
- Stored in user configuration.
- Stable when subscription settings change.
- Invalid after subscription deletion.

### Unsubscribe Link

Digest and error messages include a subscription-specific unsubscribe URL when the frontend is enabled.

The link requires no authentication.

Opening the URL must not immediately disable the subscription.

### Confirmation Page

The unsubscribe URL opens a confirmation page.

Only explicit confirmation disables the subscription.

This protects against automated email security scanners that follow links.

### Processing

After confirmation:

- Prepare the updated user configuration.
- Validate it through Core.
- Save it using the normal revision mechanism.
- Disable only the selected subscription.
- Send a final confirmation email.

The subscription remains stored with `enabled: false`.

### Failures

If saving fails:

- Keep the subscription unchanged.
- Display an error.
- Allow retry.

Repeated confirmation must not produce repeated configuration changes or confirmation emails.

### Without Frontend

Core uses a configurable unsubscribe instruction text.

The instruction may explain how to contact the administrator or submit a service desk request.

No self-service unsubscribe URL is required in file mode.

---

## Frontend Security

### Public Endpoint Protection

Use built-in rate limiting for sensitive operations.

Relevant operations include:

- Login-link requests.
- API-token authentication.
- Other authentication-related endpoints.

Combine IP-based and email-based limits where appropriate.

Return HTTP 429 when rate limits are exceeded.

An external reverse proxy may provide additional protection.

The MVP does not require Redis.

In-memory rate limiting is acceptable for the initial single-process frontend deployment.

### Internal API Security

Internal API operations require the shared API key.

Never expose this key to the Vue application.

Validate authorization on every internal request.

### Secret Handling

Do not return stored Zabbix API tokens to browsers.

Do not log tokens, passwords, internal API keys, unsubscribe tokens, or session secrets.

Avoid exposing them through exception messages or Sentry events.

Sensitive forms and API responses should use suitable cache-control behavior.

### Impersonation Security

Only verified administrators may start impersonation.

Admin rights must be derived from the real authenticated identity.

Do not trust client-provided claims of administrator status.

Impersonation must not modify or replace the administrator's original identity.

---

## HTTP API and Health Checks

### API Namespaces

Use:

- `/api/*` for frontend user operations.
- `/internal/*` for authenticated component communication.
- `/metrics` for Prometheus.
- `/health/live` for liveness.
- `/health/ready` for readiness.

Core and frontend each provide their own relevant endpoints.

### Liveness

Liveness indicates that the process is running and responsive.

It must not require a successful connection to Zabbix or SMTP.

### Readiness

Core readiness indicates that:

- Valid user configuration is available.
- The scheduler is ready to operate.

Frontend readiness indicates that:

- The backend is ready to handle requests.
- Its local configuration storage is accessible.

External Zabbix or SMTP outages do not automatically make either component unhealthy.

### Health Endpoint Security

Health endpoints do not require authentication.

They must not reveal secrets or sensitive user information.

---

## Prometheus Metrics

### General

Both Core and frontend expose Prometheus-compatible metrics.

Use maintained Prometheus libraries.

Metrics must not require persistent storage.

Counters and gauges are held in process memory.

Counter resets after restart are acceptable.

### Core Metrics

Include metrics for:

- Successful scheduled executions.
- Empty scheduled executions.
- Failed scheduled executions.
- Successful message deliveries.
- Failed message deliveries.
- Report preparation errors.
- Transport errors.
- Zabbix API errors.
- Authentication and configuration errors.
- Retry attempts.
- Digest processing duration.
- Transport delivery duration.
- Currently active workers.
- Current configuration revision.
- Configuration validation failures.
- Configuration synchronization failures.
- Scheduler readiness.

The exact metric names and types are defined during implementation.

### Frontend Metrics

Include metrics for:

- HTTP request counts and duration.
- Authentication failures.
- Login-link requests and failures.
- Configuration validation failures.
- Configuration save failures.
- Core communication failures.
- Rate limit violations.
- Unexpected application errors.

### Metric Labels

Use low-cardinality labels.

Examples:

- Component.
- Transport.
- Operation.
- Error category.
- Result status.

Do not include:

- Email addresses.
- API tokens.
- Session identifiers.
- Unsubscribe tokens.
- Arbitrary subscription IDs.
- Arbitrary exception messages.

### Retry Visibility

Temporary errors that are resolved through retry should still be observable in metrics.

Distinguish individual failed attempts from final operation failures.

---

## Logging

### General

Use standard or maintained Python logging libraries.

Default:

- JSON format.
- Standard output.

Support:

- JSON logging.
- Plain-text logging.

### Forced Debug Logging

Provide an environment variable that forces:

- Plain-text format.
- Standard output destination.

Example:

`LOG_FORCE_TEXT_STDOUT=true`

This override must take precedence over normal logging configuration.

### File Logging

Optionally support file output.

Use ready-made rotating log handlers.

Configuration includes:

- Maximum file size before rotation.
- Number of retained rotated files.

Do not implement custom log rotation logic unless necessary.

### Log Content

Include useful context such as:

- Timestamp.
- Log level.
- Component.
- Operation.
- Subscription identifier when appropriate.
- User email when appropriate.
- Error category.
- Safe diagnostic message.

Every record about a specific user names the user (email). Every record about a specific subscription names the user and the subscription ID. This includes records of nested operations, such as Zabbix request retries and delivery attempts.

Log expected operational errors without unnecessary stack traces.

The diagnostic message of an operational error must state the technical cause, not only its type, for example:

- `request to zabbix.example.com failed: ConnectError: [Errno -2] Name or service not known`.
- `connection refused`, TLS errors, timeouts with the timeout value, SMTP server replies.

The diagnostic message must not contain secrets or quoted configuration content.

Both components write an access log line per HTTP request, except health checks and `/metrics`, without secrets in paths. It can be turned off.

Every error returned to a user or to the other component is logged with its technical cause where it occurs. A generic user-facing message must never be the only trace of an error.

HTTP errors name the method, the URL (without query string and credentials), the proxy when one is used, the status and the start of the response body. SMTP errors name the server.

Outgoing HTTP requests use the proxy settings of the environment as is, or ignore them, per kind of request:

- Requests between Core and web (`proxy_use_internal`): disabled by default.
- Requests to the Zabbix API (`proxy_use_zabbix`): enabled by default.

Core does not distinguish a real Zabbix from the example stub; the setting covers both.

All outgoing TLS connections choose trusted CA certificates the same way: the `ca_file` setting of the connection, then `SSL_CERT_FILE`/`SSL_CERT_DIR` from the environment, then the certifi bundle. A missing configured CA file is a configuration error at startup.

At startup each component logs its main effective settings (endpoints, sources, directories, time zone, proxy settings) without secrets.

For retried operations (Zabbix API requests, deliveries), log every failed attempt with its number and cause, including the last one, and a separate error when the operation finally fails.

Include exception traces for unexpected programming failures.

### Audit Logging

Use standard structured logs for user actions.

Record:

- Authentication.
- Subscription creation and changes.
- Subscription deletion.
- Profile changes.
- API token replacement.
- Email changes.
- Profile deletion.
- Impersonation.
- Logout-all-sessions actions.

No separate audit database or audit file format is required.

---

## Sentry

### Optional Integration

Support Sentry independently in:

- Core Python application.
- Frontend FastAPI backend.
- Vue browser application.

Each component has its own optional DSN configuration.

When no DSN is configured, Sentry must remain disabled.

### Event Types

Send only unexpected application errors.

Do not intentionally send normal operational errors such as:

- Invalid user API tokens.
- Expected Zabbix API errors.
- Expected SMTP failures.
- Expected configuration validation failures.
- Expected authentication failures.

These are handled through logs and Prometheus metrics.

### Disabled Features

Do not enable:

- Performance tracing.
- Transaction collection.
- Profiling.
- Session replay.

### Frontend JavaScript DSN

Provide the browser DSN through a public runtime configuration endpoint.

Example:

`/api/public-config`

The endpoint must return only explicitly approved public settings.

Do not embed backend secrets in this response.

The Vue application reads the configuration and initializes Sentry when a DSN is present.

Use the same compiled Vue bundle across environments.

### Sensitive Data

Filter outgoing Sentry events to prevent leaking secrets.

Use appropriate SDK event filtering hooks.

Do not send full user configuration or Zabbix API responses as diagnostic attachments.

---

## Deployment

### General

Provide independent Docker images for:

- Core.
- Frontend.

The images must be buildable in restricted environments:

- Base images are build arguments with the current defaults (Python runtime, Node UI build stage).
- An optional build argument holds a shell command run right after each `FROM`; it is skipped when empty.
- Python packages are installed into a virtual environment, so bases that forbid system-wide pip installs work.
- The Docker Compose example passes these arguments from its environment file.

Provide a Docker Compose example for local use and testing.

The example includes a static Zabbix API stub, so the complete system can be tested without Zabbix:

- It implements only the Zabbix API methods and parameters used by Core.
- Two users; tokens and emails are set in the Compose file.
- One of them is configured as a frontend administrator, so impersonation can be tested.
- One dashboard with a Problems widget and several problems that show the effect of the widget filters and of the filter overrides.
- The stub is an example and test aid, not a reference of Zabbix behavior.

The stub and the test SMTP server are optional (a Compose profile). A real Zabbix, SMTP server and secrets are set through an environment file, without editing the Compose file.

The applications must not depend on Docker Compose-specific behavior.

### Persistent Storage

Use mounted volumes for:

- User configuration.
- Core cached configuration.
- Core subscription state.
- Frontend configuration storage.
- Optional log files.

Configuration and state directories must have suitable permissions.

### Kubernetes

Kubernetes deployment is explicitly outside the MVP scope.

Do not create mandatory Kubernetes-specific dependencies.

Kubernetes manifests, Helm charts, and production cluster deployment are handled separately.

### Process Lifecycle

Support:

- Startup validation.
- Health checks.
- Graceful SIGTERM handling.
- Bounded transport operations.
- Reliable state persistence.

The application must operate correctly when restarted to apply new Core configuration.

---

## Python and Frontend Dependencies

### Python

Use standard `requirements.txt` files with pip.

Do not require Poetry or uv.

Core and frontend backend may have separate requirements files.

Pin dependencies to reproducible versions.

### Python Compatibility

Target Python 3.10 and newer where practical.

Python 3.10 support is a soft requirement.

Do not introduce substantial complexity solely to preserve compatibility with Python 3.10.

If an important dependency or implementation requirement makes Python 3.10 impractical, document the reason and propose raising the minimum supported version.

### Vue

Use Vue 3.

Use Vite for bundling.

Maintain `package.json` and the npm lock file.

Production runtime must not require Node.js.

---

## Testing

### General

Implement both unit and integration tests.

Do not require a complete Zabbix deployment for normal automated testing.

### Unit Tests

Cover:

- Configuration loading and validation.
- ENV overrides.
- Subscription ID generation and uniqueness.
- Scheduling.
- Delivery-window calculations.
- State updates.
- Retry and backoff.
- Zabbix filtering conversion.
- Widget sorting.
- Message contract generation.
- Adapter rendering.
- Error classification.
- Session version validation.
- Unsubscribe behavior.

### Integration Tests

Cover:

- Core and frontend HTTP communication.
- Configuration validation and synchronization.
- Revision conflicts.
- Atomic YAML persistence.
- Cached configuration recovery.
- SMTP delivery through a test server.
- External adapter invocation.
- Authentication flows.
- Profile management.
- Impersonation.
- Metrics and health endpoints.

### Real Zabbix Compatibility

Perform separate compatibility checks against Zabbix 7.0.

Special attention is required for:

- Identifying a Problems widget from its URL.
- Reading widget settings through API.
- Translating widget filters to API requests.
- Acknowledged and suppressed problem handling.
- Sorting.
- Result limits.
- Access to user email media.
- Detection of disabled Zabbix users.
- Event details links.

Do not assume undocumented Zabbix behavior.

If a required API capability is unavailable, document the limitation and request an architectural decision rather than silently changing the required behavior.

### Full E2E

Automated deployment of a complete Zabbix environment is outside the initial MVP scope.

---

## Repository Structure

Use a monorepository.

The repository contains:

- Core Python source.
- Frontend FastAPI source.
- Vue source.
- Test suites.
- Docker build definitions.
- Docker Compose example.
- Configuration examples.
- Documentation.
- `README.md`.
- `AGENTS.md` (one-line pointer to `docs/AGENTS.md`).
- `docs/AI-brief.md` (this document).
- `docs/AGENTS.md`.
- `docs/AI-readme.md`.

Core and frontend use the same project release version.

### Versioning

Use Semantic Versioning.

Both components are released under one project version.

Application version, user configuration revision, and message JSON schema version are independent concepts.

Do not implement a separate Core/Frontend API compatibility negotiation mechanism in the MVP.

Compatibility errors are handled through normal HTTP errors, logs, and metrics.

---

## Documentation

### README

Provide a concise project introduction and basic startup instructions.

Keep `README.md` minimal: purpose of the project and a short Docker Compose start with a real Zabbix and SMTP server. Detailed documentation stays in `docs/AI-readme.md`.

### Configuration Documentation

Document:

- Core YAML and ENV settings.
- Frontend YAML and ENV settings.
- User YAML format.
- Subscription settings.
- Transport settings.
- SMTP configuration.
- Security requirements.

### Operational Documentation

Document:

- Running without frontend.
- Running with frontend.
- Startup behavior.
- Cache recovery.
- Graceful shutdown.
- Logs and metrics.
- Health checks.
- Common failures.
- Manual session invalidation.
- Moving configuration from file mode to frontend mode.

### Adapter Documentation

Document:

- Built-in adapter interface.
- Structured message JSON contract.
- External adapter standard input.
- Exit code behavior.
- Timeouts.
- Example external adapter integration.

### Examples

Provide working sample configurations with clearly marked placeholder values.

Examples must not contain real credentials.

---

## Requirements Maintenance

### AI-brief.md

This file describes the agreed MVP requirements.

It is not automatically more authoritative than the implementation.

Human developers may modify the implementation without immediately updating this document.

AI agents must actively check consistency between:

- Implementation.
- Tests.
- Documentation.
- AI-brief.md.

### AGENTS.md

Provide a `docs/AGENTS.md` file containing mandatory development instructions for AI agents.

At minimum, require agents to:

- Read relevant project documentation before making changes.
- Check implementation against MVP requirements.
- Avoid inventing undocumented behavior or unsupported APIs.
- Update affected tests and documentation when changing functionality.
- Review whether AI-brief.md requires changes.
- Identify inconsistencies between code and documented requirements.
- Request human agreement before resolving meaningful requirement conflicts.
- Ask for clarification when the intended behavior is ambiguous.
- Avoid silently changing established requirements.
- Preserve simple technical English throughout project artifacts.

### Source of Truth

Neither code nor AI-brief.md automatically overrides the other.

When discrepancies are found:

- Identify the discrepancy.
- Determine whether the intended behavior is clear.
- Propose a correction.
- Obtain agreement before changing requirements or resolving a meaningful conflict.
- Ask the human developer when the correct behavior is uncertain.

Do not blindly rewrite code to match the document.

Do not blindly rewrite the document to match the code.

---

## Explicitly Out of Scope

The initial MVP does not require:

- Support for multiple Zabbix instances.
- PDF reports.
- Historical event digests.
- Combining multiple widgets into one digest.
- Multiple transports for one subscription.
- Individual time zones.
- Per-user column customization.
- Per-subscription filter overrides, except the filter overrides described in Filtering.
- Custom UI-based filter editors.
- Full administrative dashboard.
- User role management beyond configured impersonation administrators.
- Separate password authentication.
- Keycloak or another external identity provider.
- Redis.
- SQLite.
- PostgreSQL.
- External task queues.
- Distributed schedulers.
- Multi-instance active-active Core processing.
- Full delivery history.
- Persistent Prometheus metric storage.
- Exactly-once delivery guarantees.
- Automatic transport fallback.
- Complete Zabbix UI replication in emails.
- Internationalization.
- Automatic deployment to Kubernetes.
- Helm charts.
- Complete automated Zabbix E2E environment.
- Automatic user configuration migration tools.
- Full configuration revision history.
- Configuration checksums.
- Full Core/Frontend API version negotiation.

These features may be considered later but must not complicate the initial implementation without a demonstrated requirement.

---

## Acceptance Criteria

### Core Without Frontend

The MVP is acceptable when Core can:

- Start with a manually prepared `users.yaml`.
- Validate the configuration.
- Read user-owned Problems widgets.
- Retrieve active problems.
- Produce HTML digests.
- Deliver them through SMTP.
- Skip empty reports.
- Send preparation-error notifications.
- Handle temporary Zabbix failures.
- Handle transport failures.
- Maintain per-subscription execution state.
- Recover eligible executions after restart.
- Expose metrics and health checks.
- Shut down gracefully.

### Core With Frontend

The MVP is acceptable when users can:

- Register using a Zabbix API token.
- Select their Zabbix email.
- Authenticate later using email login links.
- Create multiple subscriptions.
- Configure their schedules.
- Choose published transports.
- Preview real report data.
- Send test messages with synthetic data.
- Enable and disable subscriptions.
- Delete subscriptions.
- Replace their API token.
- Change their email to another eligible Zabbix email.
- Invalidate all sessions.
- Delete their profile.
- Unsubscribe using a confirmation page.

### Administrator Support

The MVP is acceptable when configured administrators can:

- Authenticate normally.
- Impersonate a user by email.
- Perform normal user actions during impersonation.
- Stop impersonation.
- Return to their own session.
- Diagnose application problems through logs and metrics.

### Configuration and Reliability

The MVP is acceptable when:

- Core works from either local YAML or frontend HTTP configuration.
- Frontend configuration changes require successful Core validation.
- Invalid configuration does not replace a working configuration.
- Configuration revisions prevent lost updates.
- Core survives temporary frontend outages using its persistent cache.
- Core restarts do not unnecessarily lose eligible scheduled deliveries.
- Delivery state remains human-readable.
- Files are written atomically.
- Transport operations respect configured timeouts.
- Unexpected application errors are observable.
- Expected operational failures are classified and reported correctly.

### Documentation

The MVP is acceptable when:

- Configuration examples are usable.
- Manual file-mode operation is documented.
- Frontend operation is documented.
- Transport extension is documented.
- Internal API behavior is documented.
- Common operational failures are documented.
- AI-brief.md and AGENTS.md exist.
- Automated tests cover the important behavioral paths.

---

## Implementation Verification Notes

The following areas require explicit verification during implementation.

They must not be treated as fully resolved solely by this specification.

### Zabbix Widget URL Identification

A standard copied Zabbix 7.0 dashboard URL identifies the dashboard only.

Decision: Core uses the first Problems widget of the dashboard (see "Problems Widget as the Filter Source").

Verify that the Zabbix API returns dashboard pages in display order and widget positions.

### Zabbix Problems Widget Semantics

Verify the exact mapping of widget fields to Zabbix API calls.

Particularly:

- Filtering.
- Acknowledged state.
- Suppressed state.
- Sorting.
- Result limits.
- Tags.

A subset of documented widget settings must not be mistaken for complete compatibility.

### Zabbix User and Email Validation

Verify how API token identity, user status, and configured email media can be retrieved under ordinary user permissions.

Confirm behavior for disabled users and revoked tokens.

Avoid assuming that every user role can access every required API method.

### Session and One-Time Token Persistence

The architecture avoids a database.

Determine the simplest reliable mechanism for enforcing:

- One-time login-link consumption.
- Expiration.
- Replay prevention.
- Concurrent login requests.

The mechanism must remain compatible with the selected deployment and file-based storage principles.

### Configuration and Identity Consistency

Ensure session metadata, profile generation identifiers, user email changes, and subscription references remain consistent when YAML is updated.

Do not introduce duplicated authoritative user databases.

### Scheduling and State Semantics

Define exact persisted slot and state field semantics before implementation.

Verify:

- Restart during report preparation.
- Restart during SMTP delivery.
- Delivery-window expiration.
- Midnight boundaries.
- DST transitions.
- Schedule changes.
- Disabled and deleted subscriptions.
- Empty reports.
- Failed report preparation.
- Successful error notifications.
- Failed transport delivery.

### Core Validation and Frontend Write Coordination

The frontend must not persist proposed configuration before successful Core validation.

Version conflicts and concurrent changes must be handled without losing user edits.

The design must clearly define how Core validates pending configuration and how the frontend commits the corresponding revision.

### Transport Contract

Define a stable schema before implementing external adapters.

Ensure external adapters receive sufficient information without being exposed to unrelated secrets.

### Security

Review:

- Cookie signing and revocation.
- Login-link handling.
- Email enumeration prevention.
- Rate limiting.
- Impersonation privilege checks.
- Secret handling in logs and Sentry.
- HTML escaping.
- External sender command execution.
- Internal API access controls.

### Python Compatibility

Verify actual dependency support for Python 3.10.

If maintaining Python 3.10 compatibility becomes unreasonable, discuss increasing the minimum supported version before making the change.

---

## Final Design Priorities

When choosing between implementation approaches, prefer:

- Simplicity over abstraction for its own sake.
- Correct behavior over premature optimization.
- Explicit configuration over hidden behavior.
- Small, understandable state files over a database.
- Standard libraries and established packages over custom infrastructure.
- Clear error reporting over silent failure.
- Existing generic mechanisms over special cases.
- Operational visibility over internal complexity.
- Easy manual operation over mandatory web dependencies.
- Extensibility without implementing unnecessary future features.

The project should be useful as a standalone Core service first, with the optional web frontend improving user experience without becoming a hard dependency for digest delivery.
