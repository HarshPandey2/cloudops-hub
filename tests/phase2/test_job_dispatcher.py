from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.job import Job
from app.services.job_dispatcher import dispatch_queued_jobs
from app.services.job_execution import claim_job, mark_job_succeeded
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


def _queued_job(session_factory, *, status="queued", **values):
    with session_factory() as db:
        job = Job(
            owner_id=1,
            job_type="system.health",
            input_payload={},
            status=status,
            **values,
        )
        db.add(job)
        db.commit()
        return job.id


def test_dispatcher_recovers_api_commit_publish_crash(session_factory):
    job_id = _queued_job(session_factory)
    submitted = []
    assert (
        dispatch_queued_jobs(session_factory, submitted.append, now=datetime.now(UTC))
        == 1
    )
    assert submitted == [str(job_id)]
    with session_factory() as db:
        job = db.query(Job).filter_by(id=job_id).one()
        assert job.status == "queued"
        assert job.dispatched_at is not None


def test_repeated_dispatch_does_not_republish_acknowledged_job(session_factory):
    job_id = _queued_job(session_factory)
    submitted = []
    now = datetime.now(UTC)
    assert dispatch_queued_jobs(session_factory, submitted.append, now=now) == 1
    assert (
        dispatch_queued_jobs(
            session_factory, submitted.append, now=now + timedelta(minutes=1)
        )
        == 0
    )
    assert submitted == [str(job_id)]


def test_concurrent_dispatchers_respect_active_lease(session_factory):
    job_id = _queued_job(session_factory)
    nested_result = []

    def enqueue(value):
        assert value == str(job_id)
        nested_result.append(dispatch_queued_jobs(session_factory, lambda _: None))

    assert dispatch_queued_jobs(session_factory, enqueue) == 1
    assert nested_result == [0]


def test_expired_lease_recovers_dispatcher_crash(session_factory):
    expired = datetime.now(UTC) - timedelta(seconds=1)
    job_id = _queued_job(
        session_factory,
        dispatch_lease_token=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        dispatch_lease_expires_at=expired,
    )
    submitted = []
    assert dispatch_queued_jobs(session_factory, submitted.append) == 1
    assert submitted == [str(job_id)]


def test_broker_failure_keeps_job_queued_and_backoff_bounded(session_factory):
    job_id = _queued_job(session_factory)

    def unavailable(_):
        raise OSError("private broker detail")

    now = datetime.now(UTC)
    assert dispatch_queued_jobs(session_factory, unavailable, now=now) == 0
    with session_factory() as db:
        job = db.query(Job).filter_by(id=job_id).one()
        assert job.status == "queued"
        assert job.dispatch_attempt_count == 1
        retry_at = job.next_dispatch_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        assert retry_at > now
        assert job.dispatch_lease_token is None
    submitted = []
    assert (
        dispatch_queued_jobs(
            session_factory,
            submitted.append,
            now=retry_at + timedelta(seconds=1),
        )
        == 1
    )
    assert submitted == [str(job_id)]


def test_cancelled_job_is_never_dispatched(session_factory):
    job_id = _queued_job(session_factory, status="cancelled")
    submitted = []
    assert dispatch_queued_jobs(session_factory, submitted.append) == 0
    assert submitted == []
    assert claim_job(session_factory, job_id) is None


def test_duplicate_delivery_claims_and_executes_once(session_factory, monkeypatch):
    job_id = _queued_job(session_factory)
    monkeypatch.setattr(job_tasks, "SessionLocal", session_factory)
    calls = []
    monkeypatch.setattr(
        job_tasks, "run_job_handler", lambda *_: calls.append("run") or {"ok": True}
    )
    job_tasks.process_job.run(str(job_id))
    job_tasks.process_job.run(str(job_id))
    assert calls == ["run"]
    with session_factory() as db:
        job = db.query(Job).filter_by(id=job_id).one()
        assert job.status == "succeeded"
        assert job.attempt_count == 1


def test_cancel_race_leaves_only_a_harmless_message(session_factory, monkeypatch):
    job_id = _queued_job(session_factory)
    submitted = []

    def cancel_during_publish(_):
        with session_factory() as db:
            db.query(Job).filter_by(id=job_id).update({"status": "cancelled"})
            db.commit()
        submitted.append("message")

    dispatch_queued_jobs(session_factory, cancel_during_publish)
    monkeypatch.setattr(job_tasks, "SessionLocal", session_factory)
    job_tasks.process_job.run(str(job_id))
    with session_factory() as db:
        job = db.query(Job).filter_by(id=job_id).one()
        assert job.status == "cancelled"
        assert job.dispatched_at is not None
        assert job.attempt_count == 0


def test_stale_worker_cannot_overwrite_recovered_attempt(session_factory):
    job_id = _queued_job(
        session_factory,
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=1),
        attempt_count=1,
    )
    current_claim = claim_job(session_factory, job_id)
    assert current_claim is not None
    assert current_claim.attempt_count == 2
    assert (
        mark_job_succeeded(
            session_factory,
            job_id,
            {"old": True},
            expected_attempt_count=1,
        )
        is False
    )
    assert (
        mark_job_succeeded(
            session_factory,
            job_id,
            {"current": True},
            expected_attempt_count=2,
        )
        is True
    )
