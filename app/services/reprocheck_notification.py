import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pipeline, ReprocheckIssue
from app.utils.github import (
    add_issue_comment,
    close_github_issue,
    create_github_issue,
    get_github_issue,
    reopen_github_issue,
)

logger = structlog.get_logger(__name__)


class ReprocheckNotificationService:
    async def handle_reprocheck_result(
        self, db: AsyncSession, pipeline: Pipeline
    ) -> None:
        reprocheck_result = (pipeline.params or {}).get("reprocheck_result", {})
        status_code = reprocheck_result.get("status_code")

        if status_code is None:
            logger.warning(
                "No status_code in reprocheck result",
                pipeline_id=str(pipeline.id),
            )
            return

        build_pipeline_id = (pipeline.params or {}).get("build_pipeline_id")
        if not build_pipeline_id:
            logger.warning(
                "No build_pipeline_id in reprocheck params",
                pipeline_id=str(pipeline.id),
            )
            return

        try:
            build_pipeline = await db.get(Pipeline, uuid.UUID(build_pipeline_id))
        except (ValueError, TypeError):
            logger.error(
                "Invalid build_pipeline_id",
                pipeline_id=str(pipeline.id),
                build_pipeline_id=build_pipeline_id,
            )
            return

        if not build_pipeline:
            logger.warning(
                "Build pipeline not found",
                pipeline_id=str(pipeline.id),
                build_pipeline_id=build_pipeline_id,
            )
            return

        git_repo = (build_pipeline.params or {}).get("repo")
        if not git_repo:
            logger.warning(
                "No git repo in build pipeline params",
                pipeline_id=str(pipeline.id),
                build_pipeline_id=build_pipeline_id,
            )
            return

        app_id = pipeline.app_id
        sha = (build_pipeline.params or {}).get("sha", "unknown")
        log_url = reprocheck_result.get("result_url") or pipeline.log_url

        if self._is_failure(status_code):
            await self._handle_failure(db, app_id, git_repo, status_code, sha, log_url)
        else:
            await self._handle_success(db, app_id, git_repo, sha, log_url)

    def _is_failure(self, status_code: str) -> bool:
        """Check if the status code indicates a failure.

        status_code == "0" means reproducible (success)
        status_code == "42" means not reproducible
        Any other non-zero means failed to build
        """
        return status_code != "0"

    def _get_failure_description(self, status_code: str) -> str:
        if status_code == "42":
            return "not reproducible"
        return "failed to build"

    async def _handle_failure(
        self,
        db: AsyncSession,
        app_id: str,
        git_repo: str,
        status_code: str,
        sha: str,
        log_url: str | None,
    ) -> None:
        stmt = select(ReprocheckIssue).where(ReprocheckIssue.app_id == app_id)
        result = await db.execute(stmt)
        existing_issue = result.scalar_one_or_none()

        failure_desc = self._get_failure_description(status_code)

        if not existing_issue:
            await self._create_new_issue(
                db, app_id, git_repo, failure_desc, sha, log_url
            )
        else:
            issue_state = await get_github_issue(git_repo, existing_issue.issue_number)

            if issue_state is None:
                logger.warning(
                    "Failed to fetch issue state, creating new issue",
                    app_id=app_id,
                    issue_number=existing_issue.issue_number,
                )
                await db.delete(existing_issue)
                await db.flush()
                await self._create_new_issue(
                    db, app_id, git_repo, failure_desc, sha, log_url
                )
                return

            if issue_state["state"] == "open":
                await self._add_failure_comment(
                    git_repo, existing_issue.issue_number, sha, log_url
                )
            elif issue_state["state_reason"] == "completed":
                await db.delete(existing_issue)
                await db.flush()
                await self._create_new_issue(
                    db, app_id, git_repo, failure_desc, sha, log_url
                )
            else:
                await reopen_github_issue(git_repo, existing_issue.issue_number)
                await self._add_reopen_comment(
                    git_repo, existing_issue.issue_number, sha, log_url
                )

    async def _handle_success(
        self,
        db: AsyncSession,
        app_id: str,
        git_repo: str,
        sha: str,
        log_url: str | None,
    ) -> None:
        stmt = select(ReprocheckIssue).where(ReprocheckIssue.app_id == app_id)
        result = await db.execute(stmt)
        existing_issue = result.scalar_one_or_none()

        if not existing_issue:
            return

        comment = "Build is now reproducible."
        if sha:
            comment += f"\n\n- Commit: {sha}"
        if log_url:
            comment += f"\n- Logs: {log_url}"

        await add_issue_comment(git_repo, existing_issue.issue_number, comment)
        await close_github_issue(git_repo, existing_issue.issue_number)
        await db.delete(existing_issue)

        logger.info(
            "Closed reprocheck issue after success",
            app_id=app_id,
            issue_number=existing_issue.issue_number,
        )

    async def _create_new_issue(
        self,
        db: AsyncSession,
        app_id: str,
        git_repo: str,
        failure_desc: str,
        sha: str,
        log_url: str | None,
    ) -> None:
        title = f"Reprocheck failed: {failure_desc}"
        body = f"Reprocheck failed with status: **{failure_desc}**\n\n"
        body += f"- Commit: {sha}\n"
        if log_url:
            body += f"- Logs: {log_url}\n"

        result = await create_github_issue(git_repo, title, body)

        if result:
            issue_url, issue_number = result
            new_issue = ReprocheckIssue(app_id=app_id, issue_number=issue_number)
            db.add(new_issue)

            logger.info(
                "Created reprocheck issue",
                app_id=app_id,
                issue_number=issue_number,
                issue_url=issue_url,
            )
        else:
            logger.error(
                "Failed to create reprocheck issue",
                app_id=app_id,
                git_repo=git_repo,
            )

    async def _add_failure_comment(
        self,
        git_repo: str,
        issue_number: int,
        sha: str,
        log_url: str | None,
    ) -> None:
        comment = "Another failure detected.\n\n"
        comment += f"- Commit: {sha}\n"
        if log_url:
            comment += f"- Logs: {log_url}\n"

        await add_issue_comment(git_repo, issue_number, comment)

        logger.info(
            "Added failure comment to reprocheck issue",
            issue_number=issue_number,
        )

    async def _add_reopen_comment(
        self,
        git_repo: str,
        issue_number: int,
        sha: str,
        log_url: str | None,
    ) -> None:
        comment = "Reopening issue due to another failure.\n\n"
        comment += f"- Commit: {sha}\n"
        if log_url:
            comment += f"- Logs: {log_url}\n"

        await add_issue_comment(git_repo, issue_number, comment)

        logger.info(
            "Added reopen comment to reprocheck issue",
            issue_number=issue_number,
        )
