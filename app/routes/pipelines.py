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
from app.pipelines import BuildPipeline
from app.providers.base import ProviderType
from app.utils.flat_manager import FlatManagerClient
from app.utils.github import (
    create_github_issue,
    create_pr_comment,
    update_commit_status,
)

logger = structlog.get_logger(__name__)
pipelines_router = APIRouter(prefix="/api", tags=["pipelines"])
security = HTTPBearer()
api_security = HTTPBearer()


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
    provider: ProviderType | None = None
    log_url: str | None = None
    build_id: str | None = None
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

        return PipelineResponse(
            id=str(pipeline.id),
            app_id=pipeline.app_id,
            status=pipeline.status,
            repo=str(pipeline.flat_manager_repo)
            if pipeline.flat_manager_repo is not None
            else None,
            params=pipeline.params,
            triggered_by=pipeline.triggered_by,
            provider=ProviderType(pipeline.provider) if pipeline.provider else None,
            log_url=pipeline.log_url,
            build_id=pipeline.build_id,
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

        updates: dict[str, Any] = {}

        if pipeline.app_id == "flathub" and "app_id" in data:
            app_id = data.get("app_id")
            if isinstance(app_id, str) and app_id:
                pipeline.app_id = app_id
                updates["app_id"] = pipeline.app_id

        if "is_extra_data" in data:
            is_extra_data = data.get("is_extra_data")
            if isinstance(is_extra_data, bool):
                pipeline.is_extra_data = is_extra_data
                updates["is_extra_data"] = pipeline.is_extra_data

        if end_of_life := data.get("end_of_life"):
            pipeline.end_of_life = end_of_life
            updates["end_of_life"] = pipeline.end_of_life

        if end_of_life_rebase := data.get("end_of_life_rebase"):
            pipeline.end_of_life_rebase = end_of_life_rebase
            updates["end_of_life_rebase"] = pipeline.end_of_life_rebase

        # Return early for field-only updates (no status change)
        if updates and "status" not in data:
            await db.commit()
            return {"status": "ok", "pipeline_id": str(pipeline_id), **updates}

        if "status" in data:
            if pipeline.status in [
                PipelineStatus.SUCCEEDED,
                PipelineStatus.PUBLISHED,
            ]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Pipeline status already finalized",
                )

            status_callback = PipelineStatusCallback(**data)
            status_value = status_callback.status.lower()

            pipeline_service = BuildPipeline()

            updated_pipeline = await pipeline_service.handle_callback(
                pipeline_id=pipeline_id, status=status_value
            )

            if updated_pipeline:
                app_id = updated_pipeline.app_id
                sha = updated_pipeline.params.get("sha")
                git_repo = updated_pipeline.params.get("repo")

                if app_id and sha and git_repo:
                    match status_value:
                        case "success":
                            description = "Build succeeded."
                            github_state = "success"
                        case "failure":
                            description = "Build failed."
                            github_state = "failure"
                        case "cancelled":
                            description = "Build cancelled."
                            github_state = "failure"
                        case _:
                            description = f"Build status: {status_value}."
                            github_state = "failure"

                    target_url = updated_pipeline.log_url
                    if not target_url:
                        logger.warning(
                            "log_url is unexpectedly None when setting final commit status",
                            pipeline_id=str(pipeline_id),
                        )
                        target_url = ""

                    if git_repo:
                        await update_commit_status(
                            sha=sha,
                            state=github_state,
                            git_repo=git_repo,
                            description=description,
                            target_url=target_url,
                        )
                    else:
                        logger.error(
                            "Missing git_repo in params. Cannot update commit status",
                            pipeline_id=str(pipeline_id),
                        )

                    if (
                        status_value == "failure"
                        and updated_pipeline.flat_manager_repo == "stable"
                    ):
                        if git_repo:
                            try:
                                title = "Stable build failed"
                                body = f"The stable build pipeline for `{app_id}` failed.\n\nCommit SHA: `{sha}`\n"
                                if target_url:
                                    body += f"Build log: {target_url}"
                                else:
                                    body += "Build log URL not available."
                                body += "\n\ncc @flathub/build-moderation"
                                await create_github_issue(
                                    git_repo=git_repo, title=title, body=body
                                )
                            except Exception as e_issue:
                                logger.error(
                                    "Failed to create GitHub issue for failed stable build",
                                    pipeline_id=str(pipeline_id),
                                    error=str(e_issue),
                                )
                        else:
                            logger.error(
                                "Missing git_repo in params. Cannot create issue for failed stable build",
                                pipeline_id=str(pipeline_id),
                            )

                    if status_value == "success":
                        if build_id := updated_pipeline.build_id:
                            try:
                                flat_manager = FlatManagerClient(
                                    url=settings.flat_manager_url,
                                    token=settings.flat_manager_token,
                                )
                                await flat_manager.commit(
                                    build_id,
                                    end_of_life=updated_pipeline.end_of_life,
                                    end_of_life_rebase=updated_pipeline.end_of_life_rebase,
                                )
                                logger.info(
                                    "Committed build",
                                    build_id=build_id,
                                    pipeline_id=str(pipeline_id),
                                )
                            except httpx.HTTPStatusError as e:
                                logger.error(
                                    "Failed to commit build",
                                    build_id=build_id,
                                    pipeline_id=str(pipeline_id),
                                    status_code=e.response.status_code,
                                    response_text=e.response.text,
                                )
                            except Exception as e:
                                logger.error(
                                    "Unexpected error while committing build",
                                    build_id=build_id,
                                    pipeline_id=str(pipeline_id),
                                    error=str(e),
                                )
                        else:
                            logger.warning(
                                "Pipeline succeeded but has no build_id, skipping commit",
                                pipeline_id=str(pipeline_id),
                            )

                    pr_number_str = updated_pipeline.params.get("pr_number")
                    if pr_number_str and git_repo:
                        try:
                            pr_number = int(pr_number_str)
                            log_url = updated_pipeline.log_url
                            comment = ""
                            if status_value == "success":
                                if build_id := updated_pipeline.build_id:
                                    flat_manager = FlatManagerClient(
                                        url=settings.flat_manager_url,
                                        token=settings.flat_manager_token,
                                    )
                                    download_url = flat_manager.get_flatpakref_url(
                                        build_id, updated_pipeline.app_id
                                    )
                                    comment = f"🚧 [Test build succeeded]({log_url}). To test this build, install it from the testing repository:\n\n```\nflatpak install --user {download_url}\n```"
                                else:
                                    comment = f"🚧 [Test build succeeded]({log_url})."
                            elif status_value == "failure":
                                comment = f"🚧 [Test build]({log_url}) failed."
                            elif status_value == "cancelled":
                                comment = f"🚧 [Test build]({log_url}) was cancelled."

                            if comment:
                                await create_pr_comment(
                                    git_repo=git_repo,
                                    pr_number=pr_number,
                                    comment=comment,
                                )
                        except ValueError:
                            logger.error(
                                "Invalid PR number. Skipping final PR comment.",
                                pr_number=pr_number_str,
                                pipeline_id=str(pipeline_id),
                            )
                        except Exception as e:
                            logger.error(
                                "Error creating final PR comment",
                                pipeline_id=str(pipeline_id),
                                error=str(e),
                            )
                    elif not git_repo:
                        logger.error(
                            "Missing git_repo in params. Cannot create PR comment",
                            pipeline_id=str(pipeline_id),
                        )

            return {
                "status": "ok",
                "pipeline_id": str(pipeline_id),
                "pipeline_status": status_value,
            }

        elif "log_url" in data:
            app_id = None
            sha = None
            pr_number_str = None
            saved_log_url = None
            git_repo = None

            async with get_db() as db:
                pipeline = await db.get(Pipeline, pipeline_id)
                if not pipeline:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Pipeline {pipeline_id} not found",
                    )

                if not secrets.compare_digest(
                    credentials.credentials, pipeline.callback_token
                ):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid callback token",
                    )

                if pipeline.log_url:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Log URL already set",
                    )

                log_url_callback = PipelineLogUrlCallback(**data)
                pipeline.log_url = log_url_callback.log_url
                await db.commit()

                saved_log_url = pipeline.log_url
                app_id = pipeline.app_id
                sha = pipeline.params.get("sha")
                pr_number_str = pipeline.params.get("pr_number")
                git_repo = pipeline.params.get("repo")

            if app_id and sha and saved_log_url:
                try:
                    target_url = saved_log_url
                    if git_repo:
                        await update_commit_status(
                            sha=sha,
                            state="pending",
                            git_repo=git_repo,
                            description="Build in progress",
                            target_url=target_url,
                        )
                    else:
                        logger.error(
                            "Missing git_repo in params. Cannot update commit status",
                            pipeline_id=str(pipeline_id),
                        )

                    if pr_number_str and git_repo:
                        try:
                            pr_number = int(pr_number_str)
                            comment = f"🚧 Started [test build]({saved_log_url})."
                            await create_pr_comment(
                                git_repo=git_repo,
                                pr_number=pr_number,
                                comment=comment,
                            )
                        except ValueError:
                            logger.error(
                                "Invalid PR number. Skipping PR comment",
                                pr_number=pr_number_str,
                                pipeline_id=str(pipeline_id),
                            )
                        except Exception as e_pr:
                            logger.error(
                                "Error creating 'Started' PR comment",
                                pipeline_id=str(pipeline_id),
                                error=str(e_pr),
                            )
                    elif not git_repo:
                        logger.error(
                            "Missing git_repo in params. Cannot create PR comment",
                            pipeline_id=str(pipeline_id),
                        )

                except Exception as e_status:
                    logger.error(
                        "Error processing GitHub updates after saving log_url",
                        pipeline_id=str(pipeline_id),
                        error=str(e_status),
                    )

            return {
                "status": "ok",
                "pipeline_id": str(pipeline_id),
                "log_url": saved_log_url,
            }

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Request must contain either 'status', 'log_url', 'app_id', 'is_extra_data', 'end_of_life', or 'end_of_life_rebase' field",
            )


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
