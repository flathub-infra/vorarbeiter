from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import BuildFailureIssue, Pipeline
from app.utils.github import (
    add_issue_comment,
    close_github_issue,
    create_github_issue,
    get_github_issue,
    list_open_github_issues,
    normalize_git_oid,
    parse_build_ref_from_log,
)

logger = structlog.get_logger(__name__)

ISSUE_TITLE = "Stable build failed"


class BuildFailureIssueService:
    async def handle_result(self, pipeline: Pipeline, status: str) -> None:
        if pipeline.flat_manager_repo != "stable" or status not in {
            "success",
            "failure",
        }:
            return

        git_repo = (pipeline.params or {}).get("repo")
        if not git_repo:
            logger.error(
                "Missing git_repo in params. Cannot track stable build issue",
                pipeline_id=str(pipeline.id),
            )
            return

        try:
            async with get_db() as db:
                state, created = await self._reserve_state(
                    db, pipeline.app_id, git_repo
                )
                if created and not await self._adopt_legacy_issue(
                    db, state, pipeline.app_id
                ):
                    return

                if status == "failure":
                    await self._handle_failure(state, pipeline, git_repo)
                else:
                    await self._handle_success(state, pipeline)
        except Exception as error:
            logger.exception(
                "Failed to update stable build issue state",
                pipeline_id=str(pipeline.id),
                app_id=pipeline.app_id,
                error=str(error),
            )

    async def get_retry_params(
        self,
        git_repo: str,
        app_id: str,
        issue_number: int,
    ) -> dict[str, Any] | None:
        try:
            async with get_db() as db:
                statement = select(BuildFailureIssue).where(
                    BuildFailureIssue.git_repo == git_repo,
                    BuildFailureIssue.app_id == app_id,
                    BuildFailureIssue.issue_number == issue_number,
                )
                state = (await db.execute(statement)).scalar_one_or_none()
                if (
                    state is None
                    or state.latest_sha is None
                    or state.latest_build_url is None
                ):
                    return None

                sha = normalize_git_oid(state.latest_sha)
                if sha is None:
                    return None
                ref = await parse_build_ref_from_log(
                    state.latest_build_url, "refs/heads/master"
                )
                return {
                    "sha": sha,
                    "repo": git_repo,
                    "ref": ref,
                    "flat_manager_repo": "stable",
                    "issue_type": "build_failure",
                }
        except Exception as error:
            logger.exception(
                "Failed to resolve stable build retry metadata",
                git_repo=git_repo,
                app_id=app_id,
                issue_number=issue_number,
                error=str(error),
            )
            return None

    async def _reserve_state(
        self,
        db: AsyncSession,
        app_id: str,
        git_repo: str,
    ) -> tuple[BuildFailureIssue, bool]:
        statement = (
            select(BuildFailureIssue)
            .where(BuildFailureIssue.app_id == app_id)
            .with_for_update()
        )
        state = (await db.execute(statement)).scalar_one_or_none()
        if state is not None:
            return state, False

        state = BuildFailureIssue(app_id=app_id, git_repo=git_repo)
        db.add(state)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            state = (await db.execute(statement)).scalar_one_or_none()
            if state is None:
                raise
            return state, False
        return state, True

    async def _adopt_legacy_issue(
        self,
        db: AsyncSession,
        state: BuildFailureIssue,
        app_id: str,
    ) -> bool:
        issues = await list_open_github_issues(state.git_repo)
        if issues is None:
            await db.delete(state)
            return False

        expected_prefix = f"The stable build pipeline for `{app_id}` failed."
        matches = [
            issue
            for issue in issues
            if "pull_request" not in issue
            and issue.get("title") == ISSUE_TITLE
            and isinstance(issue.get("body"), str)
            and issue["body"].startswith(expected_prefix)
            and isinstance(issue.get("number"), int)
        ]
        if not matches:
            return True

        adopted = max(matches, key=lambda issue: issue["number"])
        state.issue_number = adopted["number"]
        if len(matches) > 1:
            logger.warning(
                "Found duplicate open stable build issues",
                app_id=app_id,
                adopted_issue_number=state.issue_number,
                duplicate_issue_numbers=sorted(
                    issue["number"]
                    for issue in matches
                    if issue["number"] != state.issue_number
                ),
            )
        return True

    async def _handle_failure(
        self,
        state: BuildFailureIssue,
        pipeline: Pipeline,
        git_repo: str,
    ) -> None:
        if state.issue_number is None:
            await self._create_issue(state, pipeline, git_repo)
            return

        issue = await get_github_issue(state.git_repo, state.issue_number)
        if issue is None:
            return

        issue_state = issue.get("state")
        if issue_state == "open":
            comment = self._failure_comment(pipeline)
            if await add_issue_comment(state.git_repo, state.issue_number, comment):
                state.latest_sha = (pipeline.params or {}).get("sha")
                state.latest_build_url = pipeline.log_url
            return

        if issue_state == "closed":
            self._clear_active_issue(state)
            await self._create_issue(state, pipeline, git_repo)
            return

        logger.warning(
            "Unknown stable build issue state",
            app_id=pipeline.app_id,
            issue_number=state.issue_number,
            issue_state=issue_state,
        )

    async def _handle_success(
        self,
        state: BuildFailureIssue,
        pipeline: Pipeline,
    ) -> None:
        if state.issue_number is None:
            return

        issue = await get_github_issue(state.git_repo, state.issue_number)
        if issue is None:
            return

        issue_state = issue.get("state")
        if issue_state == "closed":
            self._clear_active_issue(state)
            return
        if issue_state != "open":
            logger.warning(
                "Unknown stable build issue state",
                app_id=pipeline.app_id,
                issue_number=state.issue_number,
                issue_state=issue_state,
            )
            return

        await add_issue_comment(
            state.git_repo,
            state.issue_number,
            self._success_comment(pipeline),
        )
        if await close_github_issue(state.git_repo, state.issue_number):
            self._clear_active_issue(state)

    async def _create_issue(
        self,
        state: BuildFailureIssue,
        pipeline: Pipeline,
        git_repo: str,
    ) -> None:
        result = await create_github_issue(
            git_repo=git_repo,
            title=ISSUE_TITLE,
            body=self._build_issue_body(pipeline),
        )
        if result is None:
            return

        issue_url, issue_number = result
        state.git_repo = git_repo
        state.issue_number = issue_number
        state.latest_sha = (pipeline.params or {}).get("sha")
        state.latest_build_url = pipeline.log_url
        logger.info(
            "Created stable build failure issue",
            pipeline_id=str(pipeline.id),
            issue_url=issue_url,
            issue_number=issue_number,
        )

    def _build_issue_body(self, pipeline: Pipeline) -> str:
        sha = (pipeline.params or {}).get("sha")
        body = (
            f"The stable build pipeline for `{pipeline.app_id}` failed."
            f"\n\nCommit SHA: {sha}\n"
        )

        if pipeline.log_url:
            body += f"Build log: {pipeline.log_url}"
        else:
            body += "Build log URL not available."

        if pipeline.log_url:
            body += (
                "\n\nPlease check the logs for details. "
                "If the failure was unexpected, you can retry the build "
                "by commenting `bot, retry` in this issue."
            )

        return body + "\n\ncc @flathub/build-moderation"

    def _failure_comment(self, pipeline: Pipeline) -> str:
        sha, log_url = self._display_metadata(pipeline)
        return (
            f"Another stable build failed.\n\nCommit SHA: {sha}\nBuild log: {log_url}"
        )

    def _success_comment(self, pipeline: Pipeline) -> str:
        sha, log_url = self._display_metadata(pipeline)
        return (
            "A subsequent stable build succeeded."
            f"\n\nCommit SHA: {sha}\nBuild log: {log_url}"
        )

    def _display_metadata(self, pipeline: Pipeline) -> tuple[Any, str]:
        sha = (pipeline.params or {}).get("sha") or "not available"
        log_url = pipeline.log_url or "not available"
        return sha, log_url

    def _clear_active_issue(self, state: BuildFailureIssue) -> None:
        state.issue_number = None
        state.latest_sha = None
        state.latest_build_url = None
