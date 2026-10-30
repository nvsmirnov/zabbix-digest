"""HTTP access log shared by Core and web."""

import logging

# Requests of health checks and Prometheus are not logged.
SKIP_PREFIXES = ("/health/",)
SKIP_PATHS = ("/metrics",)


def log_access(
    logger: logging.Logger, method: str, path: str, status: int, duration: float, client: str
) -> None:
    """One line per request. `path` must not contain secrets (callers mask tokens)."""
    if path in SKIP_PATHS or path.startswith(SKIP_PREFIXES):
        return
    logger.info(
        "%s %s %d %.0fms client %s", method, path, status, duration * 1000, client,
        extra={"operation": "http_access"},
    )
