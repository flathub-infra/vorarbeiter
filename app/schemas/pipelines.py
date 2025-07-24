from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, field_validator

from app.models import PipelineStatus, PipelineTrigger


class PipelineTriggerRequest(BaseModel):
    app_id: str
    params: dict[str, Any]


class PipelineSummary(BaseModel):
    id: str
    app_id: str
    status: PipelineStatus
    repo: str | None = None
    triggered_by: PipelineTrigger
    build_id: int | None = None
    commit_job_id: int | None = None
    publish_job_id: int | None = None
    update_repo_job_id: int | None = None
    repro_pipeline_id: uuid.UUID | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    published_at: datetime | None = None


class PipelineResponse(BaseModel):
    id: str
    app_id: str
    status: PipelineStatus
    repo: str | None = None
    params: dict[str, Any]
    triggered_by: PipelineTrigger
    log_url: str | None = None
    build_id: int | None = None
    commit_job_id: int | None = None
    publish_job_id: int | None = None
    update_repo_job_id: int | None = None
    repro_pipeline_id: uuid.UUID | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    published_at: datetime | None = None


class PipelineStatusCallback(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def status_must_be_valid(cls, v):
        if v not in ["success", "failure", "cancelled"]:
            raise ValueError("status must be 'success', 'failure', or 'cancelled'")
        return v


class PipelineLogUrlCallback(BaseModel):
    log_url: str


class PublishSummary(BaseModel):
    published: list[str]
    superseded: list[str]
    errors: list[dict[str, str]]
