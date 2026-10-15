from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class JobCreate(BaseModel):
    job_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    input_payload: dict[str, Any] = Field(
        max_length=100, description="Up to 100 top-level fields"
    )


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: int
    job_type: str
    input_payload: dict[str, Any]
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    retry_count: int
    error_message: str | None


class JobEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    message: str
    created_at: datetime
