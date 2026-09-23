import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, field_validator

from app.models import PipelineStatus, PipelineTrigger


class PipelineType(str, Enum):
    BUILD = "build"
    REPROCHECK = "reprocheck"


class ReprocheckStatus(str, Enum):
    REPRODUCIBLE = "reproducible"
    FAILURE = "failure"
    UNREPRODUCIBLE = "unreproducible"


class PipelineTriggerRequest(BaseModel):
    app_id: str
    params: dict[str, Any]

    @field_validator("params")
    @classmethod
    def params_must_have_valid_commit_ids(cls, v: dict[str, Any]) -> dict[str, Any]:
        from app.utils.github import validate_pipeline_commit_params

        return validate_pipeline_commit_params(v)


class PipelineSummary(BaseModel):
    id: str
    app_id: str
    type: PipelineType = PipelineType.BUILD
    status: PipelineStatus
    repo: str | None = None
    triggered_by: PipelineTrigger
    build_id: int | None = None
    commit_job_id: int | None = None
    publish_job_id: int | None = None
    update_repo_job_id: int | None = None
    repro_pipeline_id: uuid.UUID | None = None
    source_repo: str | None = None
    sha: str | None = None
    pr_number: str | None = None
    log_url: str | None = None
    failure_issue_url: str | None = None
    reprocheck_status_code: str | None = None
    reprocheck_result_url: str | None = None
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
    failure_issue_url: str | None = None
    build_id: int | None = None
    commit_job_id: int | None = None
    publish_job_id: int | None = None
    update_repo_job_id: int | None = None
    repro_pipeline_id: uuid.UUID | None = None
    total_cost: float | None = None
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


class ReproCheckEntry(BaseModel):
    app_id: str
    reprocheck_status: str | None
    result_url: str | None
    build_commit: str | None
    git_repo: str | None
    finished_at: datetime | None
    reprocheck_log_url: str | None
    repro_pipeline_id: uuid.UUID | None


class ReproducibilityData(BaseModel):
    reproducible: list[ReproCheckEntry]
    unreproducible: list[ReproCheckEntry]
    failed_to_rebuild: list[ReproCheckEntry]
    unknown: list[ReproCheckEntry]


class StatusBannerIssue(BaseModel):
    system: str
    title: str
    permalink: str
    severity: str


class StatusBannerResponse(BaseModel):
    severity: str
    label: str
    summary_status: str
    status_url: str
    issues: list[StatusBannerIssue]


class PipelineLogUrlCallback(BaseModel):
    log_url: str


class PublishSummary(BaseModel):
    published: list[str]
    superseded: list[str]
    errors: list[dict[str, str]]
