import secrets
import uuid
from datetime import datetime
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator
from sqlalchemy.future import select

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.pipelines import BuildPipeline, CallbackData
from app.utils.flat_manager import FlatManagerClient

logger = structlog.get_logger(__name__)
pipelines_router = APIRouter(prefix="/api", tags=["pipelines"])
security = HTTPBearer()
api_security = HTTPBearer()


async def update_pipeline_job_ids(pipeline: Pipeline) -> bool:
    if pipeline.commit_job_id is not None and pipeline.publish_job_id is not None:
        return False

    if not pipeline.build_id:
        return False

    try:
        flat_manager = FlatManagerClient(
            url=settings.flat_manager_url,
            token=settings.flat_manager_token,
        )
        build_info = await flat_manager.get_build_info(pipeline.build_id)
        build_data = build_info.get("build", {})

        commit_job_id = build_data.get("commit_job_id")
        publish_job_id = build_data.get("publish_job_id")

        updated = False
        if commit_job_id is not None and pipeline.commit_job_id is None:
            pipeline.commit_job_id = commit_job_id
            updated = True

        if publish_job_id is not None and pipeline.publish_job_id is None:
            pipeline.publish_job_id = publish_job_id
            updated = True

        return updated
    except Exception as e:
        logger.error(
            "Failed to fetch job IDs from flat-manager",
            pipeline_id=str(pipeline.id),
            build_id=pipeline.build_id,
            error=str(e),
        )
        return False


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(api_security),
):
    if credentials.credentials != settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
        )
    return credentials.credentials


class PipelineTriggerRequest(BaseModel):
    app_id: str
    params: dict[str, Any]


class PipelineSummary(BaseModel):
    id: str
    app_id: str
    status: PipelineStatus
    repo: str | None = None
    triggered_by: PipelineTrigger
    build_id: str | None = None
    commit_job_id: int | None = None
    publish_job_id: int | None = None
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
    build_id: str | None = None
    commit_job_id: int | None = None
    publish_job_id: int | None = None
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


@pipelines_router.post(
    "/pipelines",
    response_model=dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def trigger_pipeline(
    data: PipelineTriggerRequest,
    token: str = Depends(verify_token),
):
    pipeline_service = BuildPipeline()

    pipeline = await pipeline_service.create_pipeline(
        app_id=data.app_id,
        params=data.params,
        webhook_event_id=None,
    )

    async with get_db() as db:
        db_pipeline = await db.get(Pipeline, pipeline.id)
        if db_pipeline is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline.id} not found",
            )
        db_pipeline.triggered_by = PipelineTrigger.MANUAL
        await db.flush()
        pipeline = db_pipeline

    pipeline = await pipeline_service.start_pipeline(
        pipeline_id=pipeline.id,
    )

    return {
        "status": "created",
        "pipeline_id": str(pipeline.id),
        "app_id": pipeline.app_id,
        "pipeline_status": pipeline.status.value,
    }


@pipelines_router.get(
    "/pipelines",
    response_model=list[PipelineSummary],
    status_code=status.HTTP_200_OK,
)
async def list_pipelines(
    app_id: str | None = None,
    status_filter: PipelineStatus | None = None,
    triggered_by: PipelineTrigger | None = None,
    target_repo: str | None = None,
    limit: int | None = 10,
):
    limit = min(max(1, limit or 10), 100)

    async with get_db() as db:
        stmt = select(Pipeline).order_by(Pipeline.created_at.desc())

        if app_id is not None:
            stmt = stmt.where(Pipeline.app_id.startswith(app_id))

        if status_filter is not None:
            try:
                pipeline_status = PipelineStatus(status_filter)
                stmt = stmt.where(Pipeline.status == pipeline_status)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status value: {status_filter}. Valid values are: {[s.value for s in PipelineStatus]}",
                )

        if triggered_by is not None:
            try:
                pipeline_trigger = PipelineTrigger(triggered_by)
                stmt = stmt.where(Pipeline.triggered_by == pipeline_trigger)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid triggered_by value: {triggered_by}. Valid values are: {[t.value for t in PipelineTrigger]}",
                )

        if target_repo is not None:
            stmt = stmt.where(Pipeline.flat_manager_repo == target_repo)

        stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        pipelines = list(result.scalars().all())

        updated_job_ids = False
        for pipeline in pipelines:
            if (
                pipeline.commit_job_id is None or pipeline.publish_job_id is None
            ) and pipeline.build_id:
                if await update_pipeline_job_ids(pipeline):
                    updated_job_ids = True

        if updated_job_ids:
            await db.commit()

        return [
            PipelineSummary(
                id=str(pipeline.id),
                app_id=pipeline.app_id,
                status=pipeline.status,
                repo=str(pipeline.flat_manager_repo)
                if pipeline.flat_manager_repo is not None
                else None,
                triggered_by=pipeline.triggered_by,
                build_id=pipeline.build_id,
                commit_job_id=pipeline.commit_job_id,
                publish_job_id=pipeline.publish_job_id,
                created_at=pipeline.created_at,
                started_at=pipeline.started_at,
                finished_at=pipeline.finished_at,
                published_at=pipeline.published_at,
            )
            for pipeline in pipelines
        ]


@pipelines_router.get(
    "/pipelines/{pipeline_id}",
    response_model=PipelineResponse,
    status_code=status.HTTP_200_OK,
)
async def get_pipeline(
    pipeline_id: uuid.UUID,
):
    async with get_db() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        if (
            pipeline.commit_job_id is None or pipeline.publish_job_id is None
        ) and pipeline.build_id:
            if await update_pipeline_job_ids(pipeline):
                await db.commit()

        return PipelineResponse(
            id=str(pipeline.id),
            app_id=pipeline.app_id,
            status=pipeline.status,
            repo=str(pipeline.flat_manager_repo)
            if pipeline.flat_manager_repo is not None
            else None,
            params=pipeline.params,
            triggered_by=pipeline.triggered_by,
            log_url=pipeline.log_url,
            build_id=pipeline.build_id,
            commit_job_id=pipeline.commit_job_id,
            publish_job_id=pipeline.publish_job_id,
            created_at=pipeline.created_at,
            started_at=pipeline.started_at,
            finished_at=pipeline.finished_at,
            published_at=pipeline.published_at,
        )


@pipelines_router.post(
    "/pipelines/{pipeline_id}/callback",
    status_code=status.HTTP_200_OK,
)
async def pipeline_callback(
    pipeline_id: uuid.UUID,
    data: dict[str, Any],
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    async with get_db() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        if not secrets.compare_digest(credentials.credentials, pipeline.callback_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid callback token",
            )

    if "status" in data:
        try:
            PipelineStatusCallback(**data)
        except Exception:
            raise
    elif "log_url" in data:
        try:
            PipelineLogUrlCallback(**data)
        except Exception:
            raise
    elif not any(
        field in data
        for field in ["app_id", "is_extra_data", "end_of_life", "end_of_life_rebase"]
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request must contain either 'status', 'log_url', 'app_id', 'is_extra_data', 'end_of_life', or 'end_of_life_rebase' field",
        )

    callback_data = CallbackData(
        status=data.get("status"),
        log_url=data.get("log_url"),
        app_id=data.get("app_id"),
        is_extra_data=data.get("is_extra_data"),
        end_of_life=data.get("end_of_life"),
        end_of_life_rebase=data.get("end_of_life_rebase"),
    )

    pipeline_service = BuildPipeline()
    try:
        updated_pipeline, updates = await pipeline_service.handle_callback(
            pipeline_id=pipeline_id,
            callback_data=callback_data,
        )
    except ValueError as e:
        if "Log URL already set" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Log URL already set",
            )
        elif "Pipeline status already finalized" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Pipeline status already finalized",
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )

    return {
        "status": "ok",
        "pipeline_id": str(pipeline_id),
        **updates,
    }


@pipelines_router.get(
    "/pipelines/{pipeline_id}/log_url",
)
async def redirect_to_log_url(
    pipeline_id: uuid.UUID,
    response: Response,
):
    async with get_db() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        if not pipeline:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pipeline {pipeline_id} not found",
            )

        if not pipeline.log_url:
            response.status_code = status.HTTP_202_ACCEPTED
            response.headers["Retry-After"] = "10"
            return {"detail": "Log URL not available yet"}

        response.status_code = status.HTTP_307_TEMPORARY_REDIRECT
        response.headers["Location"] = pipeline.log_url
        return {}


class PublishSummary(BaseModel):
    published: list[str]
    superseded: list[str]
    errors: list[dict[str, str]]


@pipelines_router.post(
    "/pipelines/publish",
    response_model=PublishSummary,
    status_code=status.HTTP_200_OK,
)
async def publish_pipelines(
    token: str = Depends(verify_token),
):
    logger.info("Starting pipeline publishing process")

    published_ids = []
    superseded_ids = []
    errors = []
    now = datetime.now()

    async with get_db() as db:
        query = select(Pipeline).where(
            Pipeline.status == PipelineStatus.SUCCEEDED,
            Pipeline.flat_manager_repo.in_(["stable", "beta"]),
        )
        result = await db.execute(query)
        pipelines = list(result.scalars().all())

        pipeline_groups: dict[tuple[str, str | None], list[Pipeline]] = {}
        for pipeline in pipelines:
            if pipeline.flat_manager_repo is None:
                logger.warning(
                    "Pipeline has null flat_manager_repo, skipping",
                    pipeline_id=str(pipeline.id),
                )
                continue
            key = (pipeline.app_id, pipeline.flat_manager_repo)
            if key not in pipeline_groups:
                pipeline_groups[key] = []
            pipeline_groups[key].append(pipeline)

        for (app_id, flat_manager_repo), group in pipeline_groups.items():
            sorted_pipelines = sorted(
                group,
                key=lambda p: p.started_at if p.started_at else datetime.min,
                reverse=True,
            )
            candidate = sorted_pipelines[0]
            duplicates = sorted_pipelines[1:]

            flat_manager = FlatManagerClient(
                url=settings.flat_manager_url,
                token=settings.flat_manager_token,
            )

            for dup in duplicates:
                if dup.status != PipelineStatus.SUPERSEDED:
                    dup.status = PipelineStatus.SUPERSEDED
                    logger.info(
                        "Marked pipeline as SUPERSEDED",
                        pipeline_id=str(dup.id),
                        app_id=app_id,
                        repo=flat_manager_repo,
                    )
                if str(dup.id) not in superseded_ids:
                    superseded_ids.append(str(dup.id))

                if dup.build_id:
                    try:
                        await flat_manager.purge(dup.build_id)
                        logger.info(
                            "Purged build for superseded pipeline",
                            build_id=dup.build_id,
                            pipeline_id=str(dup.id),
                        )
                    except httpx.HTTPStatusError as purge_e:
                        logger.error(
                            "Failed to purge build for superseded pipeline",
                            build_id=dup.build_id,
                            pipeline_id=str(dup.id),
                            status_code=purge_e.response.status_code,
                            response_text=purge_e.response.text,
                        )
                    except Exception as purge_e:
                        logger.error(
                            "Unexpected error purging build for superseded pipeline",
                            build_id=dup.build_id,
                            pipeline_id=str(dup.id),
                            error=str(purge_e),
                        )

            if not candidate.build_id:
                logger.warning(
                    "Candidate Pipeline has no build_id, skipping publish",
                    pipeline_id=str(candidate.id),
                    app_id=app_id,
                    repo=flat_manager_repo,
                )
                errors.append(
                    {
                        "pipeline_id": str(candidate.id),
                        "error": "No build_id available",
                    }
                )
                continue

            build_id = candidate.build_id

            try:
                build_info = await flat_manager.get_build_info(build_id)
                fm_build_data = build_info.get("build", {})
                fm_published_state = fm_build_data.get("published_state")
                fm_repo_state = fm_build_data.get("repo_state")

                if fm_published_state is None or fm_repo_state is None:
                    raise ValueError(
                        "Missing state information in flat-manager response"
                    )

                commit_job_id = fm_build_data.get("commit_job_id")
                publish_job_id = fm_build_data.get("publish_job_id")

                if commit_job_id is not None and candidate.commit_job_id is None:
                    candidate.commit_job_id = commit_job_id

                if publish_job_id is not None and candidate.publish_job_id is None:
                    candidate.publish_job_id = publish_job_id

            except httpx.RequestError as e:
                logger.error(
                    "Failed to get build info from flat-manager",
                    build_id=build_id,
                    error=str(e),
                )
                errors.append(
                    {
                        "pipeline_id": str(candidate.id),
                        "error": f"flat-manager communication error: {e}",
                    }
                )
                continue
            except httpx.HTTPStatusError as e:
                logger.error(
                    "Failed to get build info from flat-manager",
                    build_id=build_id,
                    status_code=e.response.status_code,
                    response_text=e.response.text,
                )
                errors.append(
                    {
                        "pipeline_id": str(candidate.id),
                        "error": f"flat-manager API error: {e.response.status_code}",
                    }
                )
                continue
            except ValueError as e:
                logger.error(
                    "Failed to parse build info",
                    build_id=build_id,
                    error=str(e),
                )
                errors.append(
                    {
                        "pipeline_id": str(candidate.id),
                        "error": f"flat-manager response error: {e}",
                    }
                )
                continue
            except Exception as e:
                logger.error(
                    "Unexpected error getting build info",
                    build_id=build_id,
                    error=str(e),
                )
                errors.append(
                    {
                        "pipeline_id": str(candidate.id),
                        "error": f"Unexpected error: {e}",
                    }
                )
                continue

            if fm_published_state == 2:  # PublishedState::Published
                if candidate.status != PipelineStatus.PUBLISHED:
                    logger.info(
                        "Pipeline already marked as published in flat-manager",
                        pipeline_id=str(candidate.id),
                        build_id=build_id,
                    )
                    candidate.status = PipelineStatus.PUBLISHED
                    candidate.published_at = now
                if str(candidate.id) not in published_ids:
                    published_ids.append(str(candidate.id))
                continue

            if fm_repo_state == 3:  # RepoState::Failed
                logger.warning(
                    "Pipeline failed flat-manager validation (repo_state 3)",
                    pipeline_id=str(candidate.id),
                    build_id=build_id,
                )
                candidate.status = PipelineStatus.FAILED
                candidate.finished_at = now
                errors.append(
                    {
                        "pipeline_id": str(candidate.id),
                        "error": "Build failed in flat-manager: repo_state 3",
                    }
                )
                continue

            if (
                fm_repo_state == 0 and fm_published_state == 0
            ):  # RepoState::Uploading and not published
                logger.info(
                    "Pipeline is in Uploading state (repo_state 0), attempting to commit",
                    pipeline_id=str(candidate.id),
                    build_id=build_id,
                )
                try:
                    await flat_manager.commit(
                        build_id,
                        end_of_life=candidate.end_of_life,
                        end_of_life_rebase=candidate.end_of_life_rebase,
                    )
                    logger.info(
                        "Successfully committed build",
                        build_id=build_id,
                        pipeline_id=str(candidate.id),
                    )
                    continue
                except httpx.HTTPStatusError as e_commit:
                    logger.error(
                        "Failed to commit build",
                        build_id=build_id,
                        pipeline_id=str(candidate.id),
                        status_code=e_commit.response.status_code,
                        response_text=e_commit.response.text,
                    )
                except Exception as e_commit:
                    logger.error(
                        "Unexpected error while committing build",
                        build_id=build_id,
                        pipeline_id=str(candidate.id),
                        error=str(e_commit),
                    )

            if fm_repo_state in [
                0,
                1,
                6,
            ]:  # RepoState::{Uploading, Committing, Validating}
                logger.info(
                    "Pipeline is still processing in flat-manager, skipping for this run",
                    pipeline_id=str(candidate.id),
                    build_id=build_id,
                    repo_state=fm_repo_state,
                )
                continue

            if fm_repo_state == 2:  # RepoState::Ready
                logger.info(
                    "Attempting to publish pipeline - RepoState is Ready",
                    pipeline_id=str(candidate.id),
                    build_id=build_id,
                )
                try:
                    await flat_manager.publish(build_id)
                    logger.info(
                        "Successfully published build via flat-manager",
                        build_id=build_id,
                        pipeline_id=str(candidate.id),
                    )
                    candidate.status = PipelineStatus.PUBLISHED
                    candidate.published_at = now
                    published_ids.append(str(candidate.id))

                except httpx.HTTPStatusError as e_pub:
                    logger.error(
                        "Failed to publish build",
                        build_id=build_id,
                        pipeline_id=str(candidate.id),
                        status_code=e_pub.response.status_code,
                        response_text=e_pub.response.text,
                    )
                    errors.append(
                        {
                            "pipeline_id": str(candidate.id),
                            "error": f"Publish failed: HTTP {e_pub.response.status_code} - {e_pub.response.text}",
                        }
                    )
                except Exception as e_pub:
                    logger.error(
                        "Unexpected error while publishing build",
                        build_id=build_id,
                        pipeline_id=str(candidate.id),
                        error=str(e_pub),
                    )
                    errors.append(
                        {
                            "pipeline_id": str(candidate.id),
                            "error": f"Unexpected error during publish: {str(e_pub)}",
                        }
                    )
                continue

            else:
                logger.warning(
                    "Pipeline has unexpected flat-manager repo_state, skipping publish",
                    pipeline_id=str(candidate.id),
                    build_id=build_id,
                    repo_state=fm_repo_state,
                )
                errors.append(
                    {
                        "pipeline_id": str(candidate.id),
                        "error": f"Unexpected flat-manager repo_state: {fm_repo_state}",
                    }
                )
                continue

        await db.commit()

    logger.info(
        "Pipeline publishing completed",
        published_count=len(published_ids),
        superseded_count=len(superseded_ids),
        error_count=len(errors),
    )
    return PublishSummary(
        published=published_ids, superseded=superseded_ids, errors=errors
    )
