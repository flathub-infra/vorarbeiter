import structlog
from datetime import datetime
from typing import Dict, List, Tuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Pipeline, PipelineStatus
from app.utils.flat_manager import FlatManagerClient

logger = structlog.get_logger(__name__)


class PublishResult:
    def __init__(self) -> None:
        self.published: List[str] = []
        self.superseded: List[str] = []
        self.errors: List[Dict[str, str]] = []


class PublishingService:
    def __init__(self) -> None:
        self.flat_manager = FlatManagerClient(
            url=settings.flat_manager_url,
            token=settings.flat_manager_token,
        )

    async def publish_pipelines(self, db: AsyncSession) -> PublishResult:
        logger.info("Starting pipeline publishing process")

        result = PublishResult()
        now = datetime.now()

        pipelines = await self._get_publishable_pipelines(db)
        pipeline_groups = self._group_pipelines_for_publishing(pipelines)

        for (app_id, flat_manager_repo), group in pipeline_groups.items():
            await self._process_pipeline_group(
                db, app_id, flat_manager_repo, group, result, now
            )

        await db.commit()

        logger.info(
            "Pipeline publishing completed",
            published_count=len(result.published),
            superseded_count=len(result.superseded),
            error_count=len(result.errors),
        )

        return result

    async def _get_publishable_pipelines(self, db: AsyncSession) -> List[Pipeline]:
        query = select(Pipeline).where(
            Pipeline.status == PipelineStatus.COMMITTED,
            Pipeline.flat_manager_repo.in_(["stable", "beta"]),
        )
        result = await db.execute(query)
        return list(result.scalars().all())

    def _group_pipelines_for_publishing(
        self, pipelines: List[Pipeline]
    ) -> Dict[Tuple[str, str], List[Pipeline]]:
        pipeline_groups: Dict[Tuple[str, str], List[Pipeline]] = {}

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

        return pipeline_groups

    async def _process_pipeline_group(
        self,
        db: AsyncSession,
        app_id: str,
        flat_manager_repo: str,
        group: List[Pipeline],
        result: PublishResult,
        now: datetime,
    ) -> None:
        sorted_pipelines = sorted(
            group,
            key=lambda p: p.started_at if p.started_at else datetime.min,
            reverse=True,
        )

        candidate = sorted_pipelines[0]
        duplicates = sorted_pipelines[1:]

        await self._handle_superseded_pipelines(duplicates, result)
        await self._process_candidate_pipeline(
            candidate, app_id, flat_manager_repo, result, now
        )

    async def _handle_superseded_pipelines(
        self, pipelines: List[Pipeline], result: PublishResult
    ) -> None:
        for pipeline in pipelines:
            if pipeline.status != PipelineStatus.SUPERSEDED:
                pipeline.status = PipelineStatus.SUPERSEDED
                logger.info(
                    "Marked pipeline as SUPERSEDED",
                    pipeline_id=str(pipeline.id),
                    app_id=pipeline.app_id,
                    repo=pipeline.flat_manager_repo,
                )

            if str(pipeline.id) not in result.superseded:
                result.superseded.append(str(pipeline.id))

            if pipeline.build_id:
                await self._purge_build(pipeline.build_id, str(pipeline.id))

    async def _purge_build(self, build_id: int, pipeline_id: str) -> None:
        try:
            await self.flat_manager.purge(build_id)
            logger.info(
                "Purged build for superseded pipeline",
                build_id=build_id,
                pipeline_id=pipeline_id,
            )
        except httpx.HTTPStatusError as e:
            logger.error(
                "Failed to purge build for superseded pipeline",
                build_id=build_id,
                pipeline_id=pipeline_id,
                status_code=e.response.status_code,
                response_text=e.response.text,
            )
        except Exception as e:
            logger.error(
                "Unexpected error purging build for superseded pipeline",
                build_id=build_id,
                pipeline_id=pipeline_id,
                error=str(e),
            )

    async def _process_candidate_pipeline(
        self,
        pipeline: Pipeline,
        app_id: str,
        flat_manager_repo: str,
        result: PublishResult,
        now: datetime,
    ) -> None:
        if not pipeline.build_id:
            logger.warning(
                "Candidate Pipeline has no build_id, skipping publish",
                pipeline_id=str(pipeline.id),
                app_id=app_id,
                repo=flat_manager_repo,
            )
            result.errors.append(
                {
                    "pipeline_id": str(pipeline.id),
                    "error": "No build_id available",
                }
            )
            return

        try:
            build_info = await self._get_build_info(pipeline)
            if build_info is None:
                return

            self._update_job_ids(pipeline, build_info)
            await self._handle_build_state(pipeline, build_info, result, now)

        except Exception as e:
            self._handle_build_error(pipeline, e, result)

    async def _get_build_info(self, pipeline: Pipeline) -> Dict | None:
        try:
            assert pipeline.build_id is not None
            build_info = await self.flat_manager.get_build_info(pipeline.build_id)
            build_data = build_info.get("build", {})

            if (
                build_data.get("published_state") is None
                or build_data.get("repo_state") is None
            ):
                raise ValueError("Missing state information in flat-manager response")

            return build_data

        except httpx.RequestError as e:
            logger.error(
                "Failed to get build info from flat-manager",
                build_id=pipeline.build_id,
                error=str(e),
            )
            raise
        except httpx.HTTPStatusError as e:
            logger.error(
                "Failed to get build info from flat-manager",
                build_id=pipeline.build_id,
                status_code=e.response.status_code,
                response_text=e.response.text,
            )
            raise
        except ValueError as e:
            logger.error(
                "Failed to parse build info",
                build_id=pipeline.build_id,
                error=str(e),
            )
            raise

    def _update_job_ids(self, pipeline: Pipeline, build_data: Dict) -> None:
        commit_job_id = build_data.get("commit_job_id")
        publish_job_id = build_data.get("publish_job_id")

        if commit_job_id is not None and pipeline.commit_job_id is None:
            pipeline.commit_job_id = commit_job_id

        if publish_job_id is not None and pipeline.publish_job_id is None:
            pipeline.publish_job_id = publish_job_id

    async def _handle_build_state(
        self,
        pipeline: Pipeline,
        build_data: Dict,
        result: PublishResult,
        now: datetime,
    ) -> None:
        published_state = build_data["published_state"]
        repo_state = build_data["repo_state"]

        if published_state == 2:  # PublishedState::Published
            if pipeline.status != PipelineStatus.PUBLISHED:
                logger.info(
                    "Pipeline already marked as published in flat-manager",
                    pipeline_id=str(pipeline.id),
                    build_id=pipeline.build_id,
                )
                pipeline.status = PipelineStatus.PUBLISHED
                pipeline.published_at = now
            if str(pipeline.id) not in result.published:
                result.published.append(str(pipeline.id))
            return

        if repo_state == 3:  # RepoState::Failed
            logger.warning(
                "Pipeline failed flat-manager validation (repo_state 3)",
                pipeline_id=str(pipeline.id),
                build_id=pipeline.build_id,
            )
            pipeline.status = PipelineStatus.FAILED
            pipeline.finished_at = now
            result.errors.append(
                {
                    "pipeline_id": str(pipeline.id),
                    "error": "Build failed in flat-manager: repo_state 3",
                }
            )
            return

        if repo_state in [0, 1, 6]:  # Uploading, Committing, Validating
            logger.info(
                "Pipeline is still processing in flat-manager, skipping for this run",
                pipeline_id=str(pipeline.id),
                build_id=pipeline.build_id,
                repo_state=repo_state,
            )
            return

        if repo_state == 2:  # RepoState::Ready
            await self._try_publish_build(pipeline, result, now)
            return

        logger.warning(
            "Pipeline has unexpected flat-manager repo_state, skipping publish",
            pipeline_id=str(pipeline.id),
            build_id=pipeline.build_id,
            repo_state=repo_state,
        )
        result.errors.append(
            {
                "pipeline_id": str(pipeline.id),
                "error": f"Unexpected flat-manager repo_state: {repo_state}",
            }
        )

    async def _try_publish_build(
        self, pipeline: Pipeline, result: PublishResult, now: datetime
    ) -> None:
        assert pipeline.build_id is not None
        logger.info(
            "Attempting to publish pipeline - RepoState is Ready",
            pipeline_id=str(pipeline.id),
            build_id=pipeline.build_id,
        )
        try:
            await self.flat_manager.publish(pipeline.build_id)
            logger.info(
                "Successfully published build via flat-manager",
                build_id=pipeline.build_id,
                pipeline_id=str(pipeline.id),
            )
            pipeline.status = PipelineStatus.PUBLISHED
            pipeline.published_at = now
            result.published.append(str(pipeline.id))

        except httpx.HTTPStatusError as e:
            logger.error(
                "Failed to publish build",
                build_id=pipeline.build_id,
                pipeline_id=str(pipeline.id),
                status_code=e.response.status_code,
                response_text=e.response.text,
            )
            result.errors.append(
                {
                    "pipeline_id": str(pipeline.id),
                    "error": f"Publish failed: HTTP {e.response.status_code} - {e.response.text}",
                }
            )
        except Exception as e:
            logger.error(
                "Unexpected error while publishing build",
                build_id=pipeline.build_id,
                pipeline_id=str(pipeline.id),
                error=str(e),
            )
            result.errors.append(
                {
                    "pipeline_id": str(pipeline.id),
                    "error": f"Unexpected error during publish: {str(e)}",
                }
            )

    def _handle_build_error(
        self, pipeline: Pipeline, error: Exception, result: PublishResult
    ) -> None:
        if isinstance(error, httpx.RequestError):
            result.errors.append(
                {
                    "pipeline_id": str(pipeline.id),
                    "error": f"flat-manager communication error: {error}",
                }
            )
        elif isinstance(error, httpx.HTTPStatusError):
            result.errors.append(
                {
                    "pipeline_id": str(pipeline.id),
                    "error": f"flat-manager API error: {error.response.status_code}",
                }
            )
        elif isinstance(error, ValueError):
            result.errors.append(
                {
                    "pipeline_id": str(pipeline.id),
                    "error": f"flat-manager response error: {error}",
                }
            )
        else:
            result.errors.append(
                {
                    "pipeline_id": str(pipeline.id),
                    "error": f"Unexpected error: {error}",
                }
            )
