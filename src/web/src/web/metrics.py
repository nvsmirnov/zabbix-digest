"""Prometheus metrics of the web backend."""

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

P = "zabbix_digest_web_"


class WebMetrics:
    def __init__(self) -> None:
        r = self.registry = CollectorRegistry()
        self.requests = Counter(P + "http_requests_total", "HTTP requests.", ["method", "route", "status"], registry=r)
        self.request_duration = Histogram(
            P + "http_request_duration_seconds", "HTTP request duration.", ["method", "route"], registry=r
        )
        self.auth_failures = Counter(P + "auth_failures_total", "Failed authentication attempts.", ["reason"], registry=r)
        self.login_links = Counter(P + "login_link_requests_total", "Login link requests.", ["result"], registry=r)
        self.config_validation_failures = Counter(
            P + "config_validation_failures_total", "Configuration changes rejected by Core.", registry=r
        )
        self.config_save_failures = Counter(
            P + "config_save_failures_total", "Configuration changes not saved.", ["reason"], registry=r
        )
        self.core_failures = Counter(P + "core_failures_total", "Failed requests to Core.", ["operation"], registry=r)
        self.rate_limited = Counter(P + "rate_limited_total", "Requests rejected by rate limits.", ["limit"], registry=r)
        self.unexpected_errors = Counter(P + "unexpected_errors_total", "Unexpected application errors.", registry=r)

    def render(self) -> bytes:
        return generate_latest(self.registry)
