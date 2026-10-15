from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token
from app.auth.security import hash_password
from app.db.session import SessionLocal
from app.main import app
from app.models.job import Job, JobEvent
from app.models.user import User

client = TestClient(app)


def _new_user() -> tuple[User, dict[str, str]]:
    db = SessionLocal()
    username = f"jobs_{uuid4().hex[:12]}"
    user = User(username=username, password=hash_password("test-password-123"))
    db.add(user)
    db.commit()
    db.refresh(user)
    headers = {"Authorization": f"Bearer {create_access_token({'sub': username})}"}
    db.expunge(user)
    db.close()
    return user, headers


def _cleanup(user_ids: list[int]) -> None:
    db = SessionLocal()
    try:
        jobs = db.query(Job).filter(Job.owner_id.in_(user_ids)).all()
        job_ids = [job.id for job in jobs]
        db.query(JobEvent).filter(JobEvent.job_id.in_(job_ids)).delete(
            synchronize_session=False
        )
        db.query(Job).filter(Job.owner_id.in_(user_ids)).delete(
            synchronize_session=False
        )
        db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_job_submit_list_details_history_cancel_and_pagination():
    user, headers = _new_user()
    try:
        first = client.post(
            "/jobs",
            headers=headers,
            json={"job_type": "report.generate", "input_payload": {"range": "week"}},
        )
        second = client.post(
            "/jobs",
            headers=headers,
            json={"job_type": "image.resize", "input_payload": {"width": 32}},
        )
        assert first.status_code == 201
        assert first.json()["status"] == "queued"
        job_id = first.json()["id"]
        db = SessionLocal()
        try:
            db.query(Job).filter(Job.id == first.json()["id"]).update(
                {Job.created_at: datetime.now(UTC) - timedelta(days=1)}
            )
            db.commit()
        finally:
            db.close()

        details = client.get(f"/jobs/{job_id}", headers=headers)
        history = client.get(f"/jobs/{job_id}/history", headers=headers)
        all_jobs = client.get("/jobs?status=queued&limit=20", headers=headers)
        filtered = client.get("/jobs?status=queued&limit=1&offset=1", headers=headers)
        assert details.status_code == 200
        assert history.status_code == 200
        assert [event["event_type"] for event in history.json()] == ["submitted"]
        assert len(all_jobs.json()) == 2
        assert len(filtered.json()) == 1
        assert filtered.json()[0]["id"] == all_jobs.json()[1]["id"]

        cancelled = client.post(f"/jobs/{job_id}/cancel", headers=headers)
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["completed_at"] is not None
        assert (
            client.get("/jobs?status=cancelled", headers=headers).json()[0]["id"]
            == job_id
        )
        assert client.post(f"/jobs/{job_id}/cancel", headers=headers).status_code == 409
        assert second.status_code == 201
    finally:
        _cleanup([user.id])


def test_job_endpoints_require_authentication_and_validate_requests():
    assert client.get("/jobs").status_code == 401
    user, headers = _new_user()
    try:
        invalid = client.post(
            "/jobs", headers=headers, json={"job_type": "bad type", "input_payload": {}}
        )
        bad_filter = client.get("/jobs?status=unknown", headers=headers)
        assert invalid.status_code == 422
        assert invalid.json() == {"detail": "Validation error"}
        assert bad_filter.status_code == 422
        assert client.get("/jobs?limit=101", headers=headers).status_code == 422
    finally:
        _cleanup([user.id])


def test_job_owner_isolation_for_details_history_and_cancellation():
    owner, owner_headers = _new_user()
    other, other_headers = _new_user()
    try:
        created = client.post(
            "/jobs",
            headers=owner_headers,
            json={"job_type": "report", "input_payload": {}},
        )
        job_id = created.json()["id"]
        assert client.get(f"/jobs/{job_id}", headers=other_headers).status_code == 404
        assert (
            client.get(f"/jobs/{job_id}/history", headers=other_headers).status_code
            == 404
        )
        assert (
            client.post(f"/jobs/{job_id}/cancel", headers=other_headers).status_code
            == 404
        )
        assert client.get("/jobs", headers=other_headers).json() == []
    finally:
        _cleanup([owner.id, other.id])
