"""Web backend entry point."""

import argparse
import logging
import os
import sys

from common.version import __version__
from common.logs import LogConfig, setup_logging
from common.sentry import init_sentry
from common.settings import SettingsError
from .config import load_web_settings, settings_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zabbix-digest-web", description="Zabbix Digest web frontend backend")
    parser.add_argument("--config", "-c", help="web YAML configuration (default: $WEB_CONFIG)")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--check", action="store_true", help="validate the configuration and exit")
    args = parser.parse_args(argv)
    try:
        settings = load_web_settings(args.config or os.environ.get("WEB_CONFIG"))
    except SettingsError as exc:
        setup_logging("web", LogConfig(format="text"))
        print(str(exc), file=sys.stderr)
        return 2
    if args.check:
        print("web configuration is valid")
        return 0
    setup_logging("web", settings.log)
    init_sentry(settings.sentry_dsn, "web", __version__, settings.sentry_environment)
    log = logging.getLogger("web")
    log.info("zabbix digest web %s starting", __version__)
    for line in settings_summary(settings):
        log.info("config: %s", line)

    import uvicorn

    from .app import create_app

    try:
        app = create_app(settings)
    except ValueError as exc:
        log.error("configuration error: %s", exc)
        return 2
    uvicorn.run(
        app,
        host=settings.http.host,
        port=settings.http.port,
        log_config=None,
        access_log=False,
        proxy_headers=settings.trust_proxy_headers,
        timeout_graceful_shutdown=30,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
