import platform
import time
from collections.abc import Callable
from math import isfinite
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
)


class JobPayloadError(ValueError):
    """A job type or payload is not registered or does not match its schema."""


class RetryableJobError(RuntimeError):
    """A safe job failure that may be retried."""

    safe_message = "Temporary job execution error"


class PermanentJobError(RuntimeError):
    """A safe job failure that must not be retried."""

    safe_message = "Job failed permanently"


class DelayPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: float = Field(ge=0, le=30)


class DataProcessPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    values: list[StrictInt | StrictFloat] = Field(min_length=1, max_length=1000)


class SystemHealthPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _simulate_delay(payload: DelayPayload) -> dict[str, Any]:
    time.sleep(payload.seconds)
    return {"completed": True, "simulated_seconds": payload.seconds}


def _process_values(payload: DataProcessPayload) -> dict[str, Any]:
    values = [float(value) for value in payload.values]
    return {
        "count": len(values),
        "sum": sum(values),
        "minimum": min(values),
        "maximum": max(values),
        "average": sum(values) / len(values),
    }


def _system_health(_: SystemHealthPayload) -> dict[str, Any]:
    return {"status": "ok", "python_version": platform.python_version()}


JOB_HANDLERS: dict[str, tuple[type[BaseModel], Callable[[Any], dict[str, Any]]]] = {
    "simulation.delay": (DelayPayload, _simulate_delay),
    "data.process": (DataProcessPayload, _process_values),
    "system.health": (SystemHealthPayload, _system_health),
}


def validate_job_input(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    handler = JOB_HANDLERS.get(job_type)
    if handler is None:
        raise JobPayloadError("Unsupported job type")
    schema, _ = handler
    try:
        return schema.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise JobPayloadError("Invalid input for job type") from exc


def run_job_handler(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    validated = validate_job_input(job_type, payload)
    schema, handler = JOB_HANDLERS[job_type]
    model = schema.model_validate(validated)
    if isinstance(model, DataProcessPayload) and any(
        not isfinite(value) or abs(value) > 1e12 for value in model.values
    ):
        raise JobPayloadError("Invalid input for job type")
    return handler(model)
