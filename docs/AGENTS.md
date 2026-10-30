# Instructions for AI agents

These rules are mandatory for every AI agent that changes this repository.

## Before changing anything

- Read the relevant documentation first: `docs/AI-brief.md` (requirements), `docs/AI-readme.md`, and the files in `docs/`.
- Read `docs/decisions.md` for implementation decisions and open questions.
- Check the code you change against the MVP requirements.

## While changing

- Do not invent undocumented behavior or Zabbix API capabilities. Zabbix-specific code lives in `src/core/src/core/zabbix/`. Mark unverified Zabbix behavior in `docs/zabbix-notes.md`.
- Update affected tests and documentation together with the code.
- Review whether `docs/AI-brief.md` needs changes. Do not change established requirements silently.
- Identify inconsistencies between code, tests, documentation and `docs/AI-brief.md`, and report them.
- Ask for human agreement before resolving a meaningful requirement conflict.
- Ask for clarification when the intended behavior is ambiguous.
- Neither the code nor `docs/AI-brief.md` automatically wins. Propose a correction and wait for agreement.
- Use simple technical English in code, comments, logs, UI text, messages and documentation.
- Never log or return secrets: Zabbix tokens, SMTP passwords, internal API keys, session secrets, unsubscribe tokens.
- Error messages must state the technical cause. Use `common.errors.describe_error(exc)` instead of `type(exc).__name__`. For HTTP use `common.http.describe_request_error` and `describe_response`: they add the method, URL and proxy.
- Never return an error to a user or to the other component without logging its technical cause.
- Log records about a user must have `user` (email); about a subscription, `user` and `subscription_id`. Set them once where the operation starts with `common.logs.log_context` (blocks) or `bind_log_context` (HTTP request handlers); nested records get them automatically.
- When adding a setting that names an endpoint, a file or a directory, add it to the startup summary (`settings_summary` in `core/config.py`, `web/config.py`).
- Core is the only component that validates the complete user configuration. Do not copy validation rules into the web backend or the UI.

## Checks

- Python tests: `tests/run.sh` (all components; configuration in `tests/pytest.ini`).
- Python 3.10 compatibility: run the tests in a `python:3.10-slim` container (see `docs/AI-readme.md`).
- UI build: `npm --prefix src/web/ui run build`.

## Repository layout

Each component is a directory with its own `requirements.txt`, `src/`, `tests/` and `Dockerfile`.

- `src/common/` - shared package `common` (`src/common/src/common/`). No own tests or image; covered by component tests.
- `src/core/` - Core service, package `core`. `src/core/tests/fake_zabbix.py` is an in-memory Zabbix API fake, also used by web tests.
- `src/web/` - web backend, package `web` (`src/web/src/web/`), and the Vue 3 application (`src/web/ui/`).
- `tests/` - test environment of all components: `requirements.txt`, `pytest.ini`, `run.sh`. The tests themselves live in the component directories.
- `examples/` - sample configurations, example messages, example external adapter, `docker-compose.yml` (local example; images are built from the repository root).
- `examples/env.sample` - settings for `docker-compose.yml` with a real Zabbix and SMTP server (copied to `examples/.env`).
- `examples/zabbix-stub/` - static Zabbix API stub for the compose example. Keep it in sync with the Zabbix calls of Core; `src/core/tests/test_zabbix_stub.py` checks it.
- `docs/` - documentation: `AI-brief.md` (requirements), `AI-readme.md` (user documentation), `AGENTS.md` (this file; the root `AGENTS.md` only points here), `decisions.md`, `zabbix-notes.md`, `adapters.md`, `internal-api.md`.
- Add a dependency to the component that imports it; shared dependencies go to `src/common/requirements.txt`.
