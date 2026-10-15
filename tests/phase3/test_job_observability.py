import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import health
from app.db.base import Base
from app.models.job import Job
from app.models.job_observability import JobMetricTotals
from app.models.user import User
from app.services.job_observability import (
    JobObservabilitySnapshot,
    evaluate_job_alerts,
    load_job_observability_snapshot,
    record_service_heartbeat,
    update_job_metrics,
)


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_job_metrics_preserve_transitions_and_duration_buckets(session_factory):
    with session_factory() as db:
        owner = User(username=f"obs_{uuid4().hex}", password="unused")
        db.add(owner)
        db.flush()
        db.add(
            Job(
                owner_id=owner.id,
                job_type="data.process",
                status="queued",
                input_payload={},
            )
        )
        update_job_metrics(db, transition=("new", "queued"))
        db.flush()
        initial_totals = db.query(JobMetricTotals).one()
        assert initial_totals.queued_jobs == 1
        assert initial_totals.running_jobs == 0
        update_job_metrics(
            db,
            increments={"jobs_submitted_total": 1, "executions_started_total": 1},
            transition=("queued", "running"),
        )
        db.commit()
        update_job_metrics(
            db,
            increments={"executions_succeeded_total": 1},
            transition=("running", "succeeded"),
            execution_duration_seconds=0.4,
        )
        db.commit()

        totals = db.query(JobMetricTotals).one()
        assert totals.jobs_submitted_total == 1
        assert totals.queued_jobs == 0
        assert totals.running_jobs == 0
        assert totals.executions_succeeded_total == 1
        assert totals.execution_duration_count == 1
        assert totals.duration_le_0_1 == 0
        assert totals.duration_le_0_5 == 1


def test_snapshot_uses_persisted_heartbeats_and_alert_thresholds(
    session_factory, monkeypatch
):
    from app.services import job_observability

    now = datetime.now(UTC)
    monkeypatch.setattr(job_observability.settings, "JOB_QUEUE_AGE_ALERT_SECONDS", 30)
    monkeypatch.setattr(job_observability.settings, "JOB_FAILURE_ALERT_THRESHOLD", 2)
    monkeypatch.setattr(job_observability.settings, "JOB_DISPATCHER_STALE_SECONDS", 10)
    monkeypatch.setattr(
        job_observability.settings, "JOB_WORKER_HEARTBEAT_STALE_SECONDS", 10
    )
    record_service_heartbeat(
        session_factory,
        service="dispatcher",
        instance_id="dispatcher-test",
        now=now - timedelta(seconds=20),
    )
    record_service_heartbeat(
        session_factory,
        service="worker",
        instance_id="worker-test",
        now=now - timedelta(seconds=20),
    )
    snapshot = load_job_observability_snapshot(session_factory, now=now)
    assert snapshot.dispatcher_last_success == now - timedelta(seconds=20)
    assert snapshot.worker_last_seen == now - timedelta(seconds=20)
    assert set(evaluate_job_alerts(snapshot, now=now)) == {
        "dispatcher_stale",
        "worker_heartbeat_stale",
    }


def test_service_heartbeat_refresh_updates_existing_instance(session_factory):
    first = datetime.now(UTC)
    later = first + timedelta(seconds=5)
    record_service_heartbeat(
        session_factory,
        service="worker",
        instance_id="worker-refresh-test",
        now=first,
    )
    record_service_heartbeat(
        session_factory,
        service="worker",
        instance_id="worker-refresh-test",
        now=later,
    )

    snapshot = load_job_observability_snapshot(session_factory, now=later)
    assert snapshot.worker_last_seen == later


def test_job_health_bounds_broker_probe_and_sanitizes_timeout(monkeypatch):
    now = datetime.now(UTC)
    snapshot = JobObservabilitySnapshot(
        totals={},
        queued_age_seconds=0,
        recent_failures=0,
        dispatcher_last_success=now,
        worker_last_seen=now,
    )
    captured = {}

    class Connection:
        def ensure_connection(self, **kwargs):
            captured.update(kwargs)
            raise TimeoutError("password-marker in broker failure")

        def release(self):
            pass

    monkeypatch.setattr(health.celery_app, "connection_for_read", Connection)
    monkeypatch.setattr(health.celery_app.control, "ping", lambda **_: pytest.fail())
    monkeypatch.setattr(health, "_database_is_ready", lambda: True)
    monkeypatch.setattr(
        health, "load_job_observability_snapshot", lambda *_args, **_kwargs: snapshot
    )

    response = health.job_worker_health_check()
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert captured == {
        "max_retries": 0,
        "timeout": health.settings.HEALTH_CHECK_TIMEOUT_SECONDS,
    }
    assert payload["checks"]["broker"] == "unavailable"
    assert "password-marker" not in response.body.decode()


def test_database_health_applies_postgres_statement_timeout(monkeypatch):
    executed = []

    class Connection:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, parameters=None):
            executed.append((str(statement), parameters))

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(health, "engine", Engine())
    monkeypatch.setattr(health.settings, "HEALTH_CHECK_TIMEOUT_SECONDS", 2.5)

    assert health._database_is_ready()
    assert executed[0][1] == {"timeout": "2500ms"}


def test_queue_age_alert_excludes_delayed_retry_and_active_dispatch_lease(
    session_factory,
):
    now = datetime.now(UTC)
    with session_factory() as db:
        owner = User(username=f"queue_{uuid4().hex}", password="unused")
        db.add(owner)
        db.flush()
        job = Job(
            owner_id=owner.id,
            job_type="data.process",
            status="queued",
            input_payload={},
            created_at=now - timedelta(hours=1),
            next_dispatch_at=now + timedelta(minutes=1),
        )
        db.add(job)
        db.commit()

    snapshot = load_job_observability_snapshot(session_factory, now=now)
    assert snapshot.queued_age_seconds == 0

    with session_factory() as db:
        job = db.query(Job).one()
        job.next_dispatch_at = now - timedelta(hours=1)
        job.dispatch_lease_expires_at = now + timedelta(minutes=1)
        db.commit()
    snapshot = load_job_observability_snapshot(session_factory, now=now)
    assert snapshot.queued_age_seconds == 0

    with session_factory() as db:
        job = db.query(Job).one()
        job.dispatch_lease_expires_at = None
        db.commit()
    snapshot = load_job_observability_snapshot(session_factory, now=now)
    assert snapshot.queued_age_seconds >= 3600


def test_observability_refuses_unknown_metric_names(session_factory):
    with session_factory() as db, pytest.raises(ValueError, match="Unsupported"):
        update_job_metrics(db, increments={"arbitrary_label": 1})


def test_prometheus_job_metrics_read_the_shared_snapshot(monkeypatch):
    from app.core import metrics

    snapshot = JobObservabilitySnapshot(
        totals={
            "jobs_submitted_total": 7,
            "dispatch_attempts_total": 6,
            "dispatch_failures_total": 1,
            "executions_started_total": 4,
            "executions_succeeded_total": 3,
            "executions_failed_total": 1,
            "retries_scheduled_total": 2,
            "cancellations_total": 1,
            "queued_jobs": 2,
            "running_jobs": 1,
            "execution_duration_count": 3,
            "execution_duration_sum": 4.5,
            "duration_le_0_1": 1,
            "duration_le_0_5": 2,
            "duration_le_1": 2,
            "duration_le_5": 3,
            "duration_le_30": 3,
        },
        queued_age_seconds=40,
        recent_failures=5,
        dispatcher_last_success=None,
        worker_last_seen=None,
    )
    monkeypatch.setattr(
        metrics,
        "load_job_observability_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(metrics.JOB_METRICS_COLLECTOR, "_loaded_at", 0)
    families = {
        family.name: family for family in metrics.JOB_METRICS_COLLECTOR.collect()
    }
    assert families["cloudops_hub_jobs_submitted"].type == "counter"
    assert families["cloudops_hub_jobs_submitted"].samples[0].value == 7
    assert families["cloudops_hub_jobs"].type == "gauge"
    assert families["cloudops_hub_jobs"].samples[0].value == 2
    duration = families["cloudops_hub_job_execution_duration_seconds"]
    assert duration.type == "histogram"
    samples = {sample.name: sample for sample in duration.samples}
    assert samples["cloudops_hub_job_execution_duration_seconds_sum"].value == 4.5
    assert samples["cloudops_hub_job_execution_duration_seconds_count"].value == 3
    buckets = {
        sample.labels["le"]: sample.value
        for sample in duration.samples
        if sample.name.endswith("_bucket")
    }
    assert buckets == {
        "0.1": 1,
        "0.5": 2,
        "1.0": 2,
        "5.0": 3,
        "30.0": 3,
        "+Inf": 3,
    }
    assert (
        families["cloudops_hub_observability_database_available"].samples[0].value == 1
    )
