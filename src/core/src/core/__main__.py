"""Core command line interface."""

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys

from common.version import __version__
from common.logs import LogConfig, setup_logging
from common.sentry import init_sentry
from common.settings import SettingsError
from common.yamlio import load_yaml
from .config import CoreSettings, load_core_settings
from .errors import PreparationError, PreviewNotSupported, RenderError
from .processing import ShuttingDown
from .source import SourceError
from .transports.base import DeliveryFailed
from .users import UsersConfigError, validate_users_config

log = logging.getLogger("core")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2


def _settings(args: argparse.Namespace) -> CoreSettings:
    return load_core_settings(args.config or os.environ.get("CORE_CONFIG"))


async def _with_core(settings: CoreSettings, func):
    from .app import CoreApp

    core = CoreApp(settings)
    try:
        if not await core.source.start(core.stop):
            return EXIT_FAILURE
        return await func(core)
    finally:
        await core.aclose()


def _find(core, subscription_id: str):
    found = core.source.current.find_subscription(subscription_id)
    if found is None:
        print(f"subscription {subscription_id} not found", file=sys.stderr)
    return found


def cmd_run(args, settings) -> int:
    from .app import CoreApp

    async def main() -> int:
        core = CoreApp(settings)
        await core.run()
        return EXIT_OK

    return asyncio.run(main())


def cmd_once(args, settings) -> int:
    from .app import CoreApp

    async def main() -> int:
        only = set(args.subscription) if args.subscription else None
        core = CoreApp(settings)
        try:
            result = await core.run_once(only, force=args.force)
            known = core.source.current.subscription_ids() if core.source.current else set()
        finally:
            await core.aclose()
        for sub_id in sorted((only or set()) - known):
            print(f"subscription {sub_id} not found", file=sys.stderr)
        print(json.dumps(result, sort_keys=True))
        return EXIT_FAILURE if any(r in ("failed", "error") for r in result.values()) else EXIT_OK

    return asyncio.run(main())


def cmd_preview(args, settings) -> int:
    async def run(core) -> int:
        found = _find(core, args.subscription)
        if not found:
            return EXIT_FAILURE
        try:
            result = await core.processor.preview(*found)
        except PreparationError as exc:
            print(f"preparation error ({exc.category}): {exc.user_message} {exc.details or ''}", file=sys.stderr)
            return EXIT_FAILURE
        except PreviewNotSupported:
            print("the transport of this subscription does not support preview", file=sys.stderr)
            return EXIT_FAILURE
        except RenderError as exc:
            print(f"render error: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(result["body"])
            print(f"subject: {result['subject']}\nwritten to {args.output}")
        else:
            sys.stdout.write(result["body"])
        return EXIT_OK

    return asyncio.run(_with_core(settings, run))


def cmd_test_send(args, settings) -> int:
    async def run(core) -> int:
        found = _find(core, args.subscription)
        if not found:
            return EXIT_FAILURE
        try:
            await core.processor.test_send(*found)
        except (RenderError, DeliveryFailed, ShuttingDown) as exc:
            print(f"test message not delivered: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        print("test message sent")
        return EXIT_OK

    return asyncio.run(_with_core(settings, run))


def cmd_check_user(args, settings) -> int:
    from .zabbix.identity import validate_user

    async def run(core) -> int:
        user = next((u for u in core.source.current.users if u.email.lower() == args.email.lower()), None)
        if user is None:
            print("user not found", file=sys.stderr)
            return EXIT_FAILURE
        try:
            identity = await validate_user(core.zabbix, user.zabbix_token, user.email)
        except PreparationError as exc:
            print(f"invalid ({exc.category}): {exc.user_message} {exc.details or ''}", file=sys.stderr)
            return EXIT_FAILURE
        print(json.dumps({"userid": identity.userid, "username": identity.username, "emails": identity.emails}))
        return EXIT_OK

    return asyncio.run(_with_core(settings, run))


def cmd_validate_users(args, settings) -> int:
    path = args.file or settings.users.file
    try:
        config = validate_users_config(load_yaml(path), settings.transports.keys())
    except FileNotFoundError:
        print(f"file not found: {path}", file=sys.stderr)
        return EXIT_CONFIG
    except UsersConfigError as exc:
        print("invalid user configuration:\n  " + "\n  ".join(exc.errors), file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001
        print(f"cannot parse {path}: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"valid: revision {config.revision}, {len(config.users)} users, {len(config.subscription_ids())} subscriptions")
    return EXIT_OK


def cmd_check_config(args, settings) -> int:
    from .transports.base import build_registry

    try:
        build_registry(settings)
    except ValueError as exc:
        print(f"invalid transport configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print("core configuration is valid")
    return EXIT_OK


def cmd_new_id(args, settings) -> int:
    path = args.file or settings.users.file
    try:
        existing = validate_users_config(load_yaml(path), settings.transports.keys()).subscription_ids()
    except FileNotFoundError:
        existing = set()
    from common.ids import new_subscription_id

    print(new_subscription_id(existing, args.length))
    return EXIT_OK


def cmd_new_token(args, settings) -> int:
    print(secrets.token_urlsafe(24))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zabbix-digest-core", description="Zabbix Digest Core service")
    parser.add_argument("--config", "-c", help="Core YAML configuration (default: $CORE_CONFIG)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the service (default mode)").set_defaults(func=cmd_run)

    p = sub.add_parser("once", help="process due subscriptions once and exit")
    p.add_argument("--subscription", "-s", action="append", help="limit to these subscription IDs")
    p.add_argument(
        "--force", "-f", action="store_true",
        help="send digests of enabled subscriptions now, even if not due; the schedule state is not changed",
    )
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("preview", help="render a subscription digest with real data, do not send")
    p.add_argument("subscription")
    p.add_argument("--output", "-o", help="write HTML to this file instead of stdout")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("test-send", help="send a test message with synthetic data")
    p.add_argument("subscription")
    p.set_defaults(func=cmd_test_send)

    p = sub.add_parser("check-user", help="validate a user's Zabbix token, status and email")
    p.add_argument("email")
    p.set_defaults(func=cmd_check_user)

    p = sub.add_parser("validate-users", help="validate a user configuration file")
    p.add_argument("file", nargs="?")
    p.set_defaults(func=cmd_validate_users)

    sub.add_parser("check-config", help="validate the Core configuration").set_defaults(func=cmd_check_config)

    p = sub.add_parser("new-id", help="print an unused subscription ID")
    p.add_argument("--file", help="user configuration file")
    p.add_argument("--length", type=int, default=4)
    p.set_defaults(func=cmd_new_id)

    sub.add_parser("new-token", help="print a random token for unsubscribe links or keys").set_defaults(
        func=cmd_new_token
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = _settings(args)
    except SettingsError as exc:
        setup_logging("core", LogConfig(format="text"))
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    if args.command == "run":
        setup_logging("core", settings.log)
        init_sentry(settings.sentry_dsn, "core", __version__, settings.sentry_environment)
    else:
        setup_logging("core", LogConfig(level=settings.log.level, format="text"))
    try:
        return args.func(args, settings)
    except (SourceError, UsersConfigError) as exc:
        log.error("cannot load user configuration: %s", exc)
        return EXIT_CONFIG
    except ValueError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
