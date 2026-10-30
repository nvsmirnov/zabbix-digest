"""Prometheus metrics of Core. Labels are low-cardinality only."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

P = "zabbix_digest_core_"


class CoreMetrics:
    def __init__(self) -> None:
        r = self.registry = CollectorRegistry()
        self.executions = Counter(P + "executions_total", "Scheduled executions by final result.", ["result"], registry=r)
        self.deliveries = Counter(
            P + "deliveries_total", "Message deliveries (after retries).", ["transport", "type", "result"], registry=r
        )
        self.delivery_attempt_failures = Counter(
            P + "delivery_attempt_failures_total", "Individual failed delivery attempts.", ["transport"], registry=r
        )
        self.transport_retries = Counter(P + "transport_retries_total", "Delivery retries.", ["transport"], registry=r)
        self.preparation_errors = Counter(
            P + "preparation_errors_total", "Report preparation errors.", ["category"], registry=r
        )
        self.zabbix_errors = Counter(
            P + "zabbix_api_errors_total", "Failed Zabbix API requests (each attempt).", ["kind"], registry=r
        )
        self.zabbix_retries = Counter(P + "zabbix_api_retries_total", "Zabbix API request retries.", registry=r)
        self.unexpected_errors = Counter(P + "unexpected_errors_total", "Unexpected application errors.", registry=r)
        self.processing_duration = Histogram(
            P + "processing_duration_seconds", "Duration of scheduled subscription processing.", registry=r,
            buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
        )
        self.delivery_duration = Histogram(
            P + "delivery_duration_seconds", "Duration of single delivery attempts.", ["transport"], registry=r,
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
        )
        self.active_workers = Gauge(P + "active_workers", "Subscriptions being processed.", registry=r)
        self.config_revision = Gauge(P + "config_revision", "Applied user configuration revision.", registry=r)
        self.config_validation_failures = Counter(
            P + "config_validation_failures_total", "Rejected user configurations.", ["source"], registry=r
        )
        self.config_sync_failures = Counter(
            P + "config_sync_failures_total", "Failed configuration fetches from the frontend.", registry=r
        )
        self.scheduler_ready = Gauge(P + "scheduler_ready", "1 when the scheduler is running.", registry=r)
        self.operations = Counter(
            P + "operations_total", "Preview, test-send and identity operations.", ["operation", "result"], registry=r
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)
