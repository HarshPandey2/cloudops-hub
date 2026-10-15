from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import health as job_health
from app.auth.jwt import create_access_token
from app.core.config import settings
from app.db.base import Base
from app.db.dependency import get_db
from app.main import app
from app.models.job import Job, JobEvent
from app.models.user import User
from app.services.job_execution import claim_job
from app.services.job_handlers import PermanentJobError, RetryableJobError
from app.workers import jobs as job_tasks


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


@pytest.fixture
def api_context(session_factory, monkeypatch):
    with session_factory() as db:
        owner = User(username=f"jobs_{uuid4().hex}", password="unused")
        other = User(username=f"jobs_{uuid4().hex}", password="unused")
        db.add_all([owner, other])
        db.commit()
        owner_id, other_id = owner.id, other.id
        owner_headers = {
            "Authorization": f"Bearer {create_access_token({'sub': owner.username})}"
        }
        other_headers = {
            "Authorization": f"Bearer {create_access_token({'sub': other.username})}"
        }

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    dispatched: list[str] = []
    monkeypatch.setattr(
        job_tasks.process_job, "delay", lambda value: dispatched.append(value)
    )
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client, owner_id, other_id, owner_headers, other_headers, dispatched
    finally:
        app.dependency_overrides.clear()


def test_submission_validation_ownership_cancellation_and_retry(
    api_context, session_factory
):
    client, owner_id, _, owner_headers, other_headers, dispatched = api_context
    created = client.post(
        "/jobs",
        headers=owner_headers,
        json={"job_type": "data.process", "input_payload": {"values": [1, 2]}},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    assert created.json()["status"] == "queued"
    assert dispatched == [job_id]
    assert client.get(f"/jobs/{job_id}", headers=owner_headers).json()["id"] == job_id
    assert (
        client.get("/jobs?status=queued", headers=owner_headers).json()[0]["id"]
        == job_id
    )
    assert (
        client.get(f"/jobs/{job_id}/history", headers=owner_headers).json()[0][
            "event_type"
        ]
        == "submitted"
    )
    assert (
        client.post(
            "/jobs",
            headers=owner_headers,
            json={"job_type": "arbitrary", "input_payload": {}},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/jobs",
            headers=owner_headers,
            json={"job_type": "simulation.delay", "input_payload": {"seconds": 99}},
        ).status_code
        == 422
    )
    assert client.get(f"/jobs/{job_id}", headers=other_headers).status_code == 404
    assert (
        client.post(f"/jobs/{job_id}/cancel", headers=other_headers).status_code == 404
    )

    cancelled = client.post(f"/jobs/{job_id}/cancel", headers=owner_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    with session_factory() as db:
        failed = Job(
            owner_id=owner_id,
            job_type="data.process",
            input_payload={"values": [1]},
            status="failed",
            retry_count=0,
        )
        db.add(failed)
        db.commit()
        failed_id = str(failed.id)
        permanent = Job(
            owner_id=owner_id,
            job_type="data.process",
            input_payload={"values": [1]},
            status="failed",
            retryable=False,
        )
        db.add(permanent)
        db.commit()
        permanent_id = str(permanent.id)

    assert (
        client.post(f"/jobs/{failed_id}/retry", headers=other_headers).status_code
        == 404
    )
    retried = client.post(f"/jobs/{failed_id}/retry", headers=owner_headers)
    assert retried.status_code == 202
    assert retried.json()["status"] == "queued"
    assert retried.json()["retry_count"] == 1
    assert dispatched[-1] == failed_id
    assert (
        client.post(f"/jobs/{permanent_id}/retry", headers=owner_headers).status_code
        == 409
    )
    assert (
        client.get(f"/jobs/{failed_id}/history", headers=owner_headers).json()[0][
            "event_type"
        ]
        == "retry_scheduled"
    )


def test_broker_rejection_persists_failure_and_returns_unavailable(
    api_context, session_factory, monkeypatch
):
    client, owner_id, _, owner_headers, _, _ = api_context

    def reject(_):
        raise OSError("broker details must not be exposed")

    monkeypatch.setattr(job_tasks.process_job, "delay", reject)
    response = client.post(
        "/jobs",
        headers=owner_headers,
        json={"job_type": "system.health", "input_payload": {}},
    )
    assert response.status_code == 503
    assert "broker details" not in response.text
    with session_factory() as db:
        failed = db.query(Job).filter_by(owner_id=owner_id, status="queued").one()
        assert failed.error_message is None
        assert [
            event.event_type for event in db.query(JobEvent).filter_by(job_id=failed.id)
        ] == [
            "submitted",
            "dispatch_deferred",
        ]


def test_job_health_distinguishes_available_broker_from_missing_worker(
    api_context, monkeypatch
):
    client, _, _, owner_headers, _, _ = api_context

    class Connection:
        def ensure_connection(self, **_):
            return None

        def release(self):
            return None

    monkeypatch.setattr(
        job_health.celery_app,
        "connection_for_read",
        lambda: Connection(),
    )
    monkeypatch.setattr(job_health.celery_app.control, "ping", lambda **_: [])
    missing_worker = client.get("/health/jobs", headers=owner_headers)
    assert missing_worker.status_code == 503
    assert missing_worker.json()["checks"] == {
        "broker": "ok",
        "worker": "unavailable",
    }

    monkeypatch.setattr(
        job_health.celery_app.control,
        "ping",
        lambda **_: [{"jobworker": {"ok": "pong"}}],
    )
    healthy = client.get("/health/jobs", headers=owner_headers)
    assert healthy.status_code == 200
    assert healthy.json()["checks"]["worker"] == "available"


def test_worker_success_is_idempotent_and_persists_history(
    session_factory, monkeypatch
):
    with session_factory() as db:
        job = Job(
            owner_id=1,
            job_type="data.process",
            input_payload={"values": [2, 4]},
            status="queued",
        )
        db.add(job)
        db.commit()
        job_id = str(job.id)

    monkeypatch.setattr(job_tasks, "SessionLocal", session_factory)
    job_tasks.process_job.run(job_id)
    job_tasks.process_job.run(job_id)

    with session_factory() as db:
        job = db.query(Job).filter_by(id=UUID(job_id)).one()
        assert job.status == "succeeded"
        assert job.attempt_count == 1
        assert job.output_payload["sum"] == 6
        assert [
            event.event_type for event in db.query(JobEvent).filter_by(job_id=job.id)
        ] == [
            "started",
            "succeeded",
        ]


def test_retry_limit_and_retry_history_are_persisted(session_factory, monkeypatch):
    with session_factory() as db:
        job = Job(
            owner_id=1,
            job_type="data.process",
            input_payload={"values": [2]},
            status="queued",
            retry_count=0,
        )
        db.add(job)
        db.commit()
        job_id = str(job.id)

    monkeypatch.setattr(job_tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(
        job_tasks,
        "run_job_handler",
        lambda *_: (_ for _ in ()).throw(RetryableJobError()),
    )

    job_tasks.process_job.run(job_id)
    with session_factory() as db:
        job = db.query(Job).filter_by(id=UUID(job_id)).one()
        assert job.status == "queued"
        assert job.retry_count == 1
        retry_at = job.next_dispatch_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        assert retry_at > datetime.now(UTC)
        assert [
            event.event_type for event in db.query(JobEvent).filter_by(job_id=job.id)
        ] == [
            "started",
            "retry_scheduled",
        ]


def test_stale_running_job_can_be_reclaimed(session_factory):
    with session_factory() as db:
        job = Job(
            owner_id=1,
            job_type="system.health",
            input_payload={},
            status="running",
            started_at=datetime.now(UTC) - timedelta(hours=1),
            attempt_count=1,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    claimed = claim_job(session_factory, job_id)
    assert claimed is not None
    assert claimed.attempt_count == 2


def test_duplicate_redelivery_does_not_reclaim_a_fresh_running_job(session_factory):
    with session_factory() as db:
        job = Job(
            owner_id=1,
            job_type="system.health",
            input_payload={},
            status="running",
            started_at=datetime.now(UTC),
            attempt_count=1,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    claimed = claim_job(session_factory, job_id, redelivered=True)
    assert claimed is None


def test_retry_limit_marks_job_failed_without_another_retry(
    session_factory, monkeypatch
):
    with session_factory() as db:
        job = Job(
            owner_id=1,
            job_type="data.process",
            input_payload={"values": [2]},
            status="queued",
            retry_count=settings.JOB_MAX_RETRIES,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    monkeypatch.setattr(job_tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(
        job_tasks,
        "run_job_handler",
        lambda *_: (_ for _ in ()).throw(RetryableJobError()),
    )
    job_tasks.process_job.run(str(job_id))

    with session_factory() as db:
        job = db.query(Job).filter_by(id=job_id).one()
        assert job.status == "failed"
        assert job.retry_count == settings.JOB_MAX_RETRIES
        assert [
            event.event_type for event in db.query(JobEvent).filter_by(job_id=job.id)
        ] == [
            "started",
            "failed",
        ]


def test_permanent_failure_is_marked_not_retryable(session_factory, monkeypatch):
    with session_factory() as db:
        job = Job(
            owner_id=1,
            job_type="data.process",
            input_payload={"values": [2]},
            status="queued",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    monkeypatch.setattr(job_tasks, "SessionLocal", session_factory)
    monkeypatch.setattr(
        job_tasks,
        "run_job_handler",
        lambda *_: (_ for _ in ()).throw(PermanentJobError()),
    )
    job_tasks.process_job.run(str(job_id))

    with session_factory() as db:
        job = db.query(Job).filter_by(id=job_id).one()
        assert job.status == "failed"
        assert job.retryable is False
        assert job.retry_count == 0
