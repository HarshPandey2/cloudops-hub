import logging
import time
from uuid import UUID

from app.db.session import SessionLocal
from app.services.job_execution import (
    claim_job,
    mark_job_failed,
    mark_job_succeeded,
    schedule_job_retry,
)
from app.services.job_handlers import (
    JobPayloadError,
    PermanentJobError,
    RetryableJobError,
    run_job_handler,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger("fastapi-production-api.job_worker")


@celery_app.task(bind=True, name="app.jobs.process", max_retries=None)
def process_job(self, job_id: str) -> None:
    try:
        parsed_id = UUID(job_id)
    except (TypeError, ValueError):
        logger.warning("job_task_invalid_id")
        return

    try:
        delivery_info = self.request.delivery_info or {}
        redelivered = bool(delivery_info.get("redelivered"))
        claim = claim_job(SessionLocal, parsed_id, redelivered=redelivered)
    except Exception:
        logger.error(
            "job_claim_failed",
            extra={"job_id": str(parsed_id), "job_event": "claim_failed"},
        )
        raise
    if claim is None:
        logger.info(
            "job_execution_skipped",
            extra={"job_id": str(parsed_id), "job_event": "duplicate_or_terminal"},
        )
        return

    logger.info(
        "job_started",
        extra={
            "job_id": str(claim.id),
            "job_type": claim.job_type,
            "attempt_number": claim.attempt_count,
            "job_event": "started",
        },
    )
    execution_started = time.monotonic()
    try:
        output = run_job_handler(claim.job_type, claim.input_payload)
    except Exception as exc:
        retryable = not isinstance(exc, (JobPayloadError, PermanentJobError))
        safe_error = (
            exc.safe_message
            if isinstance(exc, RetryableJobError)
            else exc.safe_message
            if isinstance(exc, PermanentJobError)
            else "Job input is invalid"
            if isinstance(exc, JobPayloadError)
            else "Job execution failed"
        )
        retry_count = (
            schedule_job_retry(
                SessionLocal,
                claim.id,
                safe_error,
                expected_attempt_count=claim.attempt_count,
            )
            if retryable
            else None
        )
        if retry_count is not None:
            logger.warning(
                "job_retry_scheduled",
                extra={
                    "job_id": str(claim.id),
                    "job_type": claim.job_type,
                    "attempt_number": claim.attempt_count,
                    "retry_count": retry_count,
                    "job_event": "retry_scheduled",
                    "job_status": "queued",
                    "execution_duration_ms": round(
                        (time.monotonic() - execution_started) * 1000, 3
                    ),
                    "error_category": "retryable",
                },
            )
            return

        changed = mark_job_failed(
            SessionLocal,
            claim.id,
            safe_error,
            retryable=retryable,
            expected_attempt_count=claim.attempt_count,
        )
        if changed:
            logger.error(
                "job_failed",
                extra={
                    "job_id": str(claim.id),
                    "job_type": claim.job_type,
                    "attempt_number": claim.attempt_count,
                    "job_event": "failed",
                    "job_status": "failed",
                    "execution_duration_ms": round(
                        (time.monotonic() - execution_started) * 1000, 3
                    ),
                    "error_category": (
                        "invalid_payload"
                        if isinstance(exc, JobPayloadError)
                        else "permanent"
                        if isinstance(exc, PermanentJobError)
                        else "exhausted_or_unexpected"
                    ),
                },
            )
        return

    changed = mark_job_succeeded(
        SessionLocal,
        claim.id,
        output,
        expected_attempt_count=claim.attempt_count,
    )
    if changed:
        logger.info(
            "job_succeeded",
            extra={
                "job_id": str(claim.id),
                "job_type": claim.job_type,
                "attempt_number": claim.attempt_count,
                "job_event": "succeeded",
                "job_status": "succeeded",
                "execution_duration_ms": round(
                    (time.monotonic() - execution_started) * 1000, 3
                ),
            },
        )
