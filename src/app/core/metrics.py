import logging
import os
import threading
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)

from app.core.config import settings
from app.services.job_observability import (
    DURATION_BUCKETS,
    evaluate_job_alerts,
    load_job_observability_snapshot,
)

logger = logging.getLogger("fastapi-production-api.metrics")

HTTP_REQUESTS_TOTAL = Counter(
    "fastapi_production_api_http_requests_total",
    "Total HTTP requests handled by the API.",
    ("method", "route", "status_code"),
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "fastapi_production_api_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "fastapi_production_api_http_requests_in_progress",
    "HTTP requests currently being processed.",
    ("method",),
    multiprocess_mode="livesum",
)

RATE_LIMIT_DECISIONS_TOTAL = Counter(
    "fastapi_production_api_rate_limit_decisions_total",
    "Rate-limit decisions made by the API.",
    ("backend", "outcome"),
)

RATE_LIMIT_BACKEND_ERRORS_TOTAL = Counter(
    "fastapi_production_api_rate_limit_backend_errors_total",
    "Rate-limit backend errors by bounded operation category.",
    ("backend", "operation"),
)

OUTBOX_MESSAGES_TOTAL = Counter(
    "fastapi_production_api_outbox_messages_total",
    "Outbox worker message outcomes.",
    ("message_type", "outcome"),
)

OUTBOX_FAILURES_TOTAL = Counter(
    "fastapi_production_api_outbox_failures_total",
    "Outbox worker failures by bounded category.",
    ("category",),
)

OUTBOX_DELIVERY_DURATION_SECONDS = Histogram(
    "fastapi_production_api_outbox_delivery_duration_seconds",
    "Outbox delivery duration in seconds.",
    ("message_type",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

OIDC_CACHE_OPERATIONS_TOTAL = Counter(
    "fastapi_production_api_oidc_cache_operations_total",
    "OIDC public-document cache operations.",
    ("document", "outcome"),
)

OIDC_PROVIDER_FETCHES_TOTAL = Counter(
    "fastapi_production_api_oidc_provider_fetches_total",
    "OIDC public-document provider fetch outcomes.",
    ("document", "outcome"),
)

OIDC_PROVIDER_FETCH_DURATION_SECONDS = Histogram(
    "fastapi_production_api_oidc_provider_fetch_duration_seconds",
    "OIDC public-document provider fetch duration in seconds.",
    ("document",),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


class JobMetricsCollector:
    """Expose PostgreSQL-backed job state with a short, bounded scrape cache."""

    def __init__(self):
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._snapshot = None
        self._available = False

    def describe(self):
        # Keep registry registration side-effect free: the database is only
        # queried when Prometheus scrapes, never while importing the app.
        return []

    def collect(self):
        from app.db.session import SessionLocal

        with self._lock:
            if time.monotonic() - self._loaded_at >= settings.JOB_METRICS_CACHE_SECONDS:
                try:
                    self._snapshot = load_job_observability_snapshot(
                        SessionLocal,
                        statement_timeout_seconds=settings.HEALTH_CHECK_TIMEOUT_SECONDS,
                    )
                    self._available = True
                except Exception:
                    self._snapshot = None
                    self._available = False
                    logger.warning("job_metrics_database_unavailable")
                self._loaded_at = time.monotonic()
            snapshot = self._snapshot
            available = self._available

        available_metric = GaugeMetricFamily(
            "cloudops_hub_observability_database_available",
            "Whether PostgreSQL job observability state was read successfully.",
        )
        available_metric.add_metric([], int(available))
        yield available_metric
        if snapshot is None:
            return

        counters = (
            ("jobs_submitted", "Jobs accepted by the API.", "jobs_submitted_total"),
            (
                "job_dispatch_attempts",
                "Job dispatch attempts.",
                "dispatch_attempts_total",
            ),
            (
                "job_dispatch_failures",
                "Job dispatch publish failures.",
                "dispatch_failures_total",
            ),
            (
                "job_executions_started",
                "Job executions started.",
                "executions_started_total",
            ),
            (
                "job_executions_succeeded",
                "Job executions completed successfully.",
                "executions_succeeded_total",
            ),
            (
                "job_executions_failed",
                "Job executions failed terminally.",
                "executions_failed_total",
            ),
            (
                "job_retries_scheduled",
                "Job retries scheduled.",
                "retries_scheduled_total",
            ),
            (
                "job_cancellations",
                "Job cancellation requests accepted.",
                "cancellations_total",
            ),
        )
        for name, help_text, field in counters:
            metric = CounterMetricFamily(f"cloudops_hub_{name}", help_text)
            metric.add_metric([], snapshot.totals[field])
            yield metric

        jobs = GaugeMetricFamily(
            "cloudops_hub_jobs", "Current persisted jobs by state.", labels=["status"]
        )
        jobs.add_metric(["queued"], snapshot.totals["queued_jobs"])
        jobs.add_metric(["running"], snapshot.totals["running_jobs"])
        yield jobs
        for name, help_text, value in (
            (
                "cloudops_hub_job_queue_oldest_age_seconds",
                "Age of the oldest queued job.",
                snapshot.queued_age_seconds,
            ),
            (
                "cloudops_hub_job_failures_in_window",
                "Failures and retry schedules in the configured rolling window.",
                snapshot.recent_failures,
            ),
            (
                "cloudops_hub_dispatcher_last_success_timestamp_seconds",
                "Unix timestamp of the latest successful dispatcher cycle, or zero if missing.",
                snapshot.dispatcher_last_success.timestamp()
                if snapshot.dispatcher_last_success
                else 0,
            ),
            (
                "cloudops_hub_worker_last_heartbeat_timestamp_seconds",
                "Unix timestamp of the latest worker heartbeat, or zero if missing.",
                snapshot.worker_last_seen.timestamp()
                if snapshot.worker_last_seen
                else 0,
            ),
        ):
            metric = GaugeMetricFamily(name, help_text)
            metric.add_metric([], value)
            yield metric

        duration = HistogramMetricFamily(
            "cloudops_hub_job_execution_duration_seconds",
            "Persisted job execution duration.",
        )
        buckets = [
            (str(bound), snapshot.totals[field]) for bound, field in DURATION_BUCKETS
        ]
        buckets.extend([("+Inf", snapshot.totals["execution_duration_count"])])
        duration.add_metric([], buckets, snapshot.totals["execution_duration_sum"])
        yield duration

        active_alerts = set(evaluate_job_alerts(snapshot))
        alerts = GaugeMetricFamily(
            "cloudops_hub_job_alert",
            "Whether a bounded job reliability alert is active.",
            labels=["alert"],
        )
        for alert in (
            "queue_age_exceeded",
            "repeated_job_failures",
            "dispatcher_stale",
            "worker_heartbeat_stale",
        ):
            alerts.add_metric([alert], int(alert in active_alerts))
        yield alerts


JOB_METRICS_COLLECTOR = JobMetricsCollector()
REGISTRY.register(JOB_METRICS_COLLECTOR)


def render_metrics() -> tuple[bytes, str]:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        registry.register(JOB_METRICS_COLLECTOR)
    else:
        registry = REGISTRY

    return generate_latest(registry), CONTENT_TYPE_LATEST
