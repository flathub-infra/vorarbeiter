import logging
import secrets
import uuid
from datetime import datetime
from typing import Any
import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, field_validator
from sqlalchemy.future import select

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.pipelines import BuildPipeline
from app.utils.flat_manager import FlatManagerClient
from app.utils.github import create_pr_comment, update_commit_status

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
    status: str
    repo: str | None = None
    triggered_by: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    published_at: datetime | None = None


class PipelineResponse(BaseModel):
    id: str
    app_id: str
    status: str
    repo: str | None = None
    params: dict[str, Any]
    triggered_by: str
    provider: str | None = None
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
    status_filter: str | None = None,
    triggered_by: str | None = None,
    target_repo: str | None = None,
    limit: int | None = 10,
):
    limit = min(max(1, limit or 10), 100)

    async with get_db() as db:
        stmt = select(Pipeline).order_by(Pipeline.created_at.desc())

        if app_id is not None:
            stmt = stmt.where(Pipeline.app_id == app_id)

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
                status=pipeline.status.value,
                repo=str(pipeline.flat_manager_repo)
                if pipeline.flat_manager_repo is not None
                else None,
                triggered_by=pipeline.triggered_by.value,
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
            status=pipeline.status.value,
            repo=str(pipeline.flat_manager_repo)
            if pipeline.flat_manager_repo is not None
            else None,
            params=pipeline.params,
            triggered_by=pipeline.triggered_by.value,
            provider=pipeline.provider,
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

        if pipeline.app_id == "flathub" and "app_id" in data:
            app_id = data.get("app_id")
            if isinstance(app_id, str) and app_id:
                pipeline.app_id = app_id
                await db.commit()

                return {
                    "status": "ok",
                    "pipeline_id": str(pipeline_id),
                    "app_id": app_id,
                }

        if "status" in data:
            if pipeline.status in [
                PipelineStatus.SUCCEEDED,
                PipelineStatus.FAILED,
                PipelineStatus.CANCELLED,
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
                        logging.warning(
                            f"Pipeline {pipeline_id}: log_url is unexpectedly None when setting final commit status."
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
                        logging.error(
                            f"Pipeline {pipeline_id}: Missing git_repo in params. Cannot update commit status."
                        )

                    if status_value == "success":
                        if build_id := updated_pipeline.build_id:
                            try:
                                flat_manager = FlatManagerClient(
                                    url=settings.flat_manager_url,
                                    token=settings.flat_manager_token,
                                )
                                await flat_manager.commit(build_id)
                                logging.info(
                                    f"Committed build {build_id} for pipeline {pipeline_id}"
                                )
                            except httpx.HTTPStatusError as e:
                                logging.error(
                                    f"Failed to commit build {build_id} for pipeline {pipeline_id}. Status: {e.response.status_code}, Response: {e.response.text}"
                                )
                            except Exception as e:
                                logging.error(
                                    f"An unexpected error occurred while committing build {build_id} for pipeline {pipeline_id}: {e}"
                                )
                        else:
                            logging.warning(
                                f"Pipeline {pipeline_id} succeeded but has no build_id, skipping commit."
                            )

                    if status_value != "success":
                        if build_id := updated_pipeline.build_id:
                            try:
                                flat_manager = FlatManagerClient(
                                    url=settings.flat_manager_url,
                                    token=settings.flat_manager_token,
                                )
                                await flat_manager.purge(build_id)
                                logging.info(
                                    f"Purged build {build_id} (status: {status_value}) for pipeline {pipeline_id}"
                                )
                            except httpx.HTTPStatusError as e:
                                logging.error(
                                    f"Failed to purge build {build_id} for pipeline {pipeline_id}. Status: {e.response.status_code}, Response: {e.response.text}"
                                )
                            except Exception as e:
                                logging.error(
                                    f"An unexpected error occurred while purging build {build_id} for pipeline {pipeline_id}: {e}"
                                )
                        else:
                            logging.warning(
                                f"Pipeline {pipeline_id} finished with status '{status_value}' but has no build_id, skipping purge."
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
                            logging.error(
                                f"Invalid pr_number '{pr_number_str}' for pipeline {pipeline_id}. Skipping final PR comment."
                            )
                        except Exception as e:
                            logging.error(
                                f"Error creating final PR comment for pipeline {pipeline_id}: {e}"
                            )
                    elif not git_repo:
                        logging.error(
                            f"Pipeline {pipeline_id}: Missing git_repo in params. Cannot create PR comment."
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
                        logging.error(
                            f"Pipeline {pipeline_id}: Missing git_repo in params. Cannot update commit status."
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
                            logging.error(
                                f"Invalid pr_number '{pr_number_str}' for pipeline {pipeline_id}. Skipping PR comment."
                            )
                        except Exception as e_pr:
                            logging.error(
                                f"Error creating 'Started' PR comment for pipeline {pipeline_id}: {e_pr}"
                            )
                    elif not git_repo:
                        logging.error(
                            f"Pipeline {pipeline_id}: Missing git_repo in params. Cannot create PR comment."
                        )

                except Exception as e_status:
                    logging.error(
                        f"Error processing GitHub updates after saving log_url for pipeline {pipeline_id}: {e_status}"
                    )

            return {
                "status": "ok",
                "pipeline_id": str(pipeline_id),
                "log_url": saved_log_url,
            }

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Request must contain either 'status', 'log_url', or 'app_id' field",
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
    logging.info("Starting pipeline publishing process")

    published_ids = []
    superseded_ids = []
    errors = []

    async with get_db() as db:
        query = select(Pipeline).where(
            Pipeline.status == PipelineStatus.SUCCEEDED,
            Pipeline.flat_manager_repo.in_(["stable", "beta"]),
        )
        result = await db.execute(query)
        pipelines = list(result.scalars().all())

        pipeline_groups: dict[tuple[str, str | None], list[Pipeline]] = {}
        for pipeline in pipelines:
            key = (pipeline.app_id, pipeline.flat_manager_repo)
            if key not in pipeline_groups:
                pipeline_groups[key] = []
            pipeline_groups[key].append(pipeline)

        for (app_id, flat_manager_repo), group in pipeline_groups.items():
            if flat_manager_repo is None:
                continue

            sorted_pipelines = sorted(
                group,
                key=lambda p: p.started_at if p.started_at else datetime.min,
                reverse=True,
            )

            candidate = sorted_pipelines[0]
            duplicates = sorted_pipelines[1:]

            if not candidate.build_id:
                logging.warning(
                    f"Pipeline {candidate.id} has no build_id, skipping publish"
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
                flat_manager = FlatManagerClient(
                    url=settings.flat_manager_url,
                    token=settings.flat_manager_token,
                )
                await flat_manager.publish(build_id)

                candidate.status = PipelineStatus.PUBLISHED
                candidate.published_at = datetime.now()
                published_ids.append(str(candidate.id))
                logging.info(
                    f"Published build {build_id} for pipeline {candidate.id} ({app_id} to {flat_manager_repo})"
                )

                for dup in duplicates:
                    dup.status = PipelineStatus.SUPERSEDED
                    superseded_ids.append(str(dup.id))
                    logging.info(
                        f"Marked pipeline {dup.id} as SUPERSEDED (older version of {app_id} to {flat_manager_repo})"
                    )

                    if dup.build_id:
                        try:
                            await flat_manager.purge(dup.build_id)
                            logging.info(
                                f"Purged build {dup.build_id} for superseded pipeline {dup.id}"
                            )
                        except httpx.HTTPStatusError as purge_e:
                            logging.error(
                                f"Failed to purge build {dup.build_id} for superseded pipeline {dup.id}. Status: {purge_e.response.status_code}, Response: {purge_e.response.text}"
                            )
                        except Exception as purge_e:
                            logging.error(
                                f"An unexpected error occurred while purging build {dup.build_id} for superseded pipeline {dup.id}: {purge_e}"
                            )
                    else:
                        logging.warning(
                            f"Superseded pipeline {dup.id} has no build_id, cannot purge."
                        )

            except httpx.HTTPStatusError as e:
                try:
                    error_data = json.loads(e.response.text)
                    error_type = error_data.get("error-type")
                    current_state = error_data.get("current-state")

                    if (
                        e.response.status_code == 400
                        and error_type == "wrong-published-state"
                    ):
                        logging.warning(
                            f"Pipeline {candidate.id} (Build {build_id}) already published. Marking as published."
                        )
                        if candidate.status != PipelineStatus.PUBLISHED:
                            candidate.status = PipelineStatus.PUBLISHED
                            candidate.published_at = datetime.now()
                        if str(candidate.id) not in published_ids:
                            published_ids.append(str(candidate.id))

                    elif (
                        e.response.status_code == 400
                        and error_type == "wrong-repo-state"
                        and current_state == "validating"
                    ):
                        logging.info(
                            f"Pipeline {candidate.id} (Build {build_id}) is still validating. Skipping for this publish run."
                        )
                    elif (
                        e.response.status_code == 400
                        and error_type == "wrong-repo-state"
                        and current_state == "uploading"
                    ):
                        logging.info(
                            f"Pipeline {candidate.id} (Build {build_id}) is still uploading. Skipping for this publish run."
                        )
                    else:
                        logging.error(
                            f"Failed to publish build {build_id} for pipeline {candidate.id}. Status: {e.response.status_code}, Response: {e.response.text}"
                        )
                        errors.append(
                            {
                                "pipeline_id": str(candidate.id),
                                "error": f"HTTP error: {e.response.status_code} - {e.response.text}",
                            }
                        )

                except (json.JSONDecodeError, AttributeError):
                    logging.error(
                        f"Failed to publish build {build_id} for pipeline {candidate.id}. Status: {e.response.status_code}, Response: {e.response.text} (Could not parse error details)"
                    )
                    errors.append(
                        {
                            "pipeline_id": str(candidate.id),
                            "error": f"HTTP error: {e.response.status_code} - {e.response.text}",
                        }
                    )

            except Exception as e:
                logging.error(
                    f"An unexpected error occurred while publishing build {build_id} for pipeline {candidate.id}: {e}"
                )
                errors.append(
                    {
                        "pipeline_id": str(candidate.id),
                        "error": f"Unexpected error: {str(e)}",
                    }
                )

        await db.commit()

    logging.info(
        f"Pipeline publishing completed: {len(published_ids)} published, {len(superseded_ids)} superseded, {len(errors)} errors"
    )

    return PublishSummary(
        published=published_ids, superseded=superseded_ids, errors=errors
    )
