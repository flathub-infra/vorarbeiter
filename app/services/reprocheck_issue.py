import uuid
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Pipeline, ReprocheckIssue, ReprocheckIssueStatus
from app.utils.github import add_issue_comment, close_github_issue, create_github_issue

logger = structlog.get_logger(__name__)


REPROCHECK_STATUS_REPRODUCIBLE = "0"
REPROCHECK_STATUS_NOT_REPRODUCIBLE = "42"
REPROCHECK_STATUS_FAILED = "1"


class ReprocheckIssueService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_issue_for_app(self, app_id: str) -> ReprocheckIssue | None:
        stmt = select(ReprocheckIssue).where(ReprocheckIssue.app_id == app_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def handle_reprocheck_result(
        self,
        reprocheck_pipeline: Pipeline,
        build_pipeline: Pipeline,
        status_code: str,
    ) -> None:
        app_id = build_pipeline.app_id
        git_repo = build_pipeline.params.get("repo")

        if not git_repo:
            logger.warning(
                "Build pipeline missing git_repo, cannot track reprocheck issue",
                reprocheck_pipeline_id=str(reprocheck_pipeline.id),
                build_pipeline_id=str(build_pipeline.id),
                app_id=app_id,
            )
            return

        existing_issue = await self.get_issue_for_app(app_id)

        if status_code == REPROCHECK_STATUS_REPRODUCIBLE:
            if existing_issue and existing_issue.status == ReprocheckIssueStatus.OPEN:
                await self._close_issue_as_resolved(
                    existing_issue, reprocheck_pipeline, build_pipeline
                )
            else:
                logger.debug(
                    "Build is reproducible and no open issue exists",
                    app_id=app_id,
                    reprocheck_pipeline_id=str(reprocheck_pipeline.id),
                )
        elif status_code in (
            REPROCHECK_STATUS_NOT_REPRODUCIBLE,
            REPROCHECK_STATUS_FAILED,
        ):
            if existing_issue and existing_issue.status == ReprocheckIssueStatus.OPEN:
                await self._update_issue_with_new_failure(
                    existing_issue, reprocheck_pipeline, build_pipeline, status_code
                )
            elif existing_issue and existing_issue.status == ReprocheckIssueStatus.CLOSED:
                await self._reopen_issue_with_new_failure(
                    existing_issue, reprocheck_pipeline, build_pipeline, status_code, git_repo
                )
            else:
                await self._create_new_issue(
                    reprocheck_pipeline, build_pipeline, status_code, git_repo
                )
        else:
            logger.warning(
                "Unknown reprocheck status code",
                status_code=status_code,
                reprocheck_pipeline_id=str(reprocheck_pipeline.id),
            )

    async def _create_new_issue(
        self,
        reprocheck_pipeline: Pipeline,
        build_pipeline: Pipeline,
        status_code: str,
        git_repo: str,
    ) -> None:
        app_id = build_pipeline.app_id
        title = self._get_issue_title(status_code, app_id)
        body = self._get_issue_body(
            status_code, app_id, build_pipeline, reprocheck_pipeline, is_new=True
        )

        issue_url = await create_github_issue(git_repo, title, body)
        if not issue_url:
            logger.error(
                "Failed to create reprocheck issue",
                app_id=app_id,
                git_repo=git_repo,
                status_code=status_code,
            )
            return

        issue_number = self._extract_issue_number(issue_url)
        if not issue_number:
            logger.error(
                "Failed to extract issue number from URL",
                issue_url=issue_url,
                app_id=app_id,
            )
            return

        reprocheck_issue = ReprocheckIssue(
            app_id=app_id,
            git_repo=git_repo,
            issue_number=issue_number,
            issue_url=issue_url,
            status=ReprocheckIssueStatus.OPEN,
            last_status_code=status_code,
            last_pipeline_id=reprocheck_pipeline.id,
        )
        self.db.add(reprocheck_issue)
        await self.db.commit()

        logger.info(
            "Created reprocheck issue",
            app_id=app_id,
            git_repo=git_repo,
            issue_number=issue_number,
            issue_url=issue_url,
            status_code=status_code,
        )

    async def _update_issue_with_new_failure(
        self,
        existing_issue: ReprocheckIssue,
        reprocheck_pipeline: Pipeline,
        build_pipeline: Pipeline,
        status_code: str,
    ) -> None:
        if existing_issue.last_pipeline_id == reprocheck_pipeline.id:
            logger.debug(
                "Skipping duplicate update for reprocheck issue",
                app_id=existing_issue.app_id,
                issue_number=existing_issue.issue_number,
                pipeline_id=str(reprocheck_pipeline.id),
            )
            return

        comment = self._get_update_comment(
            status_code, build_pipeline, reprocheck_pipeline
        )

        success = await add_issue_comment(
            existing_issue.git_repo,
            existing_issue.issue_number,
            comment,
            check_duplicates=True,
        )

        if success:
            existing_issue.last_status_code = status_code
            existing_issue.last_pipeline_id = reprocheck_pipeline.id
            existing_issue.updated_at = datetime.now()
            await self.db.commit()

            logger.info(
                "Updated reprocheck issue with new failure",
                app_id=existing_issue.app_id,
                issue_number=existing_issue.issue_number,
                status_code=status_code,
            )
        else:
            logger.error(
                "Failed to add comment to reprocheck issue",
                app_id=existing_issue.app_id,
                issue_number=existing_issue.issue_number,
            )

    async def _reopen_issue_with_new_failure(
        self,
        existing_issue: ReprocheckIssue,
        reprocheck_pipeline: Pipeline,
        build_pipeline: Pipeline,
        status_code: str,
        git_repo: str,
    ) -> None:
        app_id = build_pipeline.app_id
        title = self._get_issue_title(status_code, app_id)
        body = self._get_issue_body(
            status_code, app_id, build_pipeline, reprocheck_pipeline, is_new=True
        )
        body += f"\n\n*This is a recurrence of a previously resolved issue (#{existing_issue.issue_number}).*"

        issue_url = await create_github_issue(git_repo, title, body)
        if not issue_url:
            logger.error(
                "Failed to create new reprocheck issue after previous was closed",
                app_id=app_id,
                git_repo=git_repo,
                status_code=status_code,
            )
            return

        issue_number = self._extract_issue_number(issue_url)
        if not issue_number:
            logger.error(
                "Failed to extract issue number from URL",
                issue_url=issue_url,
                app_id=app_id,
            )
            return

        existing_issue.issue_number = issue_number
        existing_issue.issue_url = issue_url
        existing_issue.status = ReprocheckIssueStatus.OPEN
        existing_issue.last_status_code = status_code
        existing_issue.last_pipeline_id = reprocheck_pipeline.id
        existing_issue.updated_at = datetime.now()
        existing_issue.closed_at = None
        await self.db.commit()

        logger.info(
            "Created new reprocheck issue (previous was closed)",
            app_id=app_id,
            git_repo=git_repo,
            issue_number=issue_number,
            issue_url=issue_url,
            status_code=status_code,
            previous_issue_number=existing_issue.issue_number,
        )

    async def _close_issue_as_resolved(
        self,
        existing_issue: ReprocheckIssue,
        reprocheck_pipeline: Pipeline,
        build_pipeline: Pipeline,
    ) -> None:
        comment = self._get_resolved_comment(build_pipeline, reprocheck_pipeline)

        await add_issue_comment(
            existing_issue.git_repo,
            existing_issue.issue_number,
            comment,
        )

        success = await close_github_issue(
            existing_issue.git_repo,
            existing_issue.issue_number,
        )

        if success:
            existing_issue.status = ReprocheckIssueStatus.CLOSED
            existing_issue.last_status_code = REPROCHECK_STATUS_REPRODUCIBLE
            existing_issue.last_pipeline_id = reprocheck_pipeline.id
            existing_issue.updated_at = datetime.now()
            existing_issue.closed_at = datetime.now()
            await self.db.commit()

            logger.info(
                "Closed reprocheck issue as resolved",
                app_id=existing_issue.app_id,
                issue_number=existing_issue.issue_number,
            )
        else:
            logger.error(
                "Failed to close reprocheck issue",
                app_id=existing_issue.app_id,
                issue_number=existing_issue.issue_number,
            )

    def _get_issue_title(self, status_code: str, app_id: str) -> str:
        if status_code == REPROCHECK_STATUS_NOT_REPRODUCIBLE:
            return f"Build is not reproducible"
        else:
            return f"Reproducibility check failed"

    def _get_issue_body(
        self,
        status_code: str,
        app_id: str,
        build_pipeline: Pipeline,
        reprocheck_pipeline: Pipeline,
        is_new: bool = True,
    ) -> str:
        sha = build_pipeline.params.get("sha", "unknown")
        reprocheck_result = reprocheck_pipeline.params.get("reprocheck_result", {})
        result_url = reprocheck_result.get("result_url")

        if status_code == REPROCHECK_STATUS_NOT_REPRODUCIBLE:
            status_description = (
                "The build for this app is **not reproducible**. "
                "This means the build output differs from the officially published build."
            )
        else:
            status_description = (
                "The reproducibility check **failed** for this app. "
                "This means the reprocheck job could not complete successfully."
            )

        body = f"{status_description}\n\n"
        body += "**Build Information:**\n"
        body += f"- App ID: `{app_id}`\n"
        body += f"- Commit: {sha}\n"

        if build_pipeline.log_url:
            body += f"- Build log: {build_pipeline.log_url}\n"

        if reprocheck_pipeline.log_url:
            body += f"- Reprocheck log: {reprocheck_pipeline.log_url}\n"

        if result_url:
            body += f"- Diffoscope results: {result_url}\n"

        diffoscope_url = f"{settings.base_url}/diffoscope/{reprocheck_pipeline.id}"
        body += f"- Diffoscope viewer: {diffoscope_url}\n"

        body += "\n---\n\n"
        body += (
            "Reproducible builds help ensure that the published app matches its source code. "
            "For more information, see the [Flathub reproducible builds documentation]"
            "(https://docs.flathub.org/docs/for-app-authors/maintenance/#reproducible-builds).\n\n"
        )
        body += "cc @flathub/build-moderation"

        return body

    def _get_update_comment(
        self,
        status_code: str,
        build_pipeline: Pipeline,
        reprocheck_pipeline: Pipeline,
    ) -> str:
        sha = build_pipeline.params.get("sha", "unknown")
        reprocheck_result = reprocheck_pipeline.params.get("reprocheck_result", {})
        result_url = reprocheck_result.get("result_url")

        if status_code == REPROCHECK_STATUS_NOT_REPRODUCIBLE:
            status_text = "still not reproducible"
        else:
            status_text = "failed again"

        comment = f"The reproducibility check {status_text} for a new build.\n\n"
        comment += f"- Commit: {sha}\n"

        if build_pipeline.log_url:
            comment += f"- Build log: {build_pipeline.log_url}\n"

        if reprocheck_pipeline.log_url:
            comment += f"- Reprocheck log: {reprocheck_pipeline.log_url}\n"

        if result_url:
            comment += f"- Diffoscope results: {result_url}\n"

        diffoscope_url = f"{settings.base_url}/diffoscope/{reprocheck_pipeline.id}"
        comment += f"- Diffoscope viewer: {diffoscope_url}\n"

        return comment

    def _get_resolved_comment(
        self,
        build_pipeline: Pipeline,
        reprocheck_pipeline: Pipeline,
    ) -> str:
        sha = build_pipeline.params.get("sha", "unknown")

        comment = "The build is now **reproducible**. Closing this issue.\n\n"
        comment += f"- Commit: {sha}\n"

        if build_pipeline.log_url:
            comment += f"- Build log: {build_pipeline.log_url}\n"

        if reprocheck_pipeline.log_url:
            comment += f"- Reprocheck log: {reprocheck_pipeline.log_url}\n"

        return comment

    def _extract_issue_number(self, issue_url: str) -> int | None:
        try:
            return int(issue_url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError, AttributeError):
            return None
