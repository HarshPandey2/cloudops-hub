import logging

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.redis import get_redis_client
from app.db.session import engine
from app.workers.celery_app import celery_app

router = APIRouter()
logger = logging.getLogger("fastapi-production-api.health")


def _database_is_ready() -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.warning("database_readiness_check_failed", exc_info=True)
        return False


async def _redis_is_ready() -> bool:
    try:
        return bool(await get_redis_client().ping())
    except (RedisError, OSError, TimeoutError):
        logger.warning("redis_readiness_check_failed")
        return False


@router.get("/health/live")
def liveness_check():
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness_check():
    database_ready = await run_in_threadpool(_database_is_ready)
    if settings.RATE_LIMIT_BACKEND != "redis":
        if database_ready:
            return {
                "status": "ok",
                "checks": {"database": "ok"},
            }

        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "checks": {"database": "unavailable"},
            },
        )

    redis_ready = await _redis_is_ready()
    checks = {
        "database": "ok" if database_ready else "unavailable",
        "redis": "ok" if redis_ready else "unavailable",
    }
    if database_ready and redis_ready:
        return {
            "status": "ok",
            "checks": checks,
        }

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "error",
            "checks": checks,
        },
    )


@router.get("/health/jobs")
def job_worker_health_check():
    connection = celery_app.connection_for_read()
    try:
        connection.ensure_connection(max_retries=0)
        broker_ready = True
    except Exception:
        broker_ready = False
        logger.warning("job_broker_health_check_failed")
    finally:
        connection.release()

    worker_ready = False
    if broker_ready:
        try:
            worker_ready = bool(celery_app.control.ping(timeout=1.0, limit=1))
        except Exception:
            logger.warning("job_worker_health_check_failed")

    checks = {
        "broker": "ok" if broker_ready else "unavailable",
        "worker": "available" if worker_ready else "unavailable",
    }
    if broker_ready and worker_ready:
        return {"status": "ok", "checks": checks}
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "error", "checks": checks},
    )


@router.get("/health", deprecated=True)
def health_check():
    return liveness_check()


@router.get("/health/db", deprecated=True)
def database_health_check():
    if _database_is_ready():
        return {
            "status": "ok",
            "database": "connected",
        }

    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "error",
            "database": "disconnected",
        },
    )
