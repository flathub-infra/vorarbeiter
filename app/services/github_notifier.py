from typing import Optional

import structlog

from app.config import settings
from app.models import Pipeline
from app.utils.flat_manager import FlatManagerClient
from app.utils.github import (
    create_github_issue,
    create_pr_comment,
    update_commit_status,
)

logger = structlog.get_logger(__name__)


class GitHubNotifier:
    def __init__(self, flat_manager_client: Optional[FlatManagerClient] = None):
        self.flat_manager = flat_manager_client

    async def notify_build_status(
        self,
        pipeline: Pipeline,
        status: str,
        log_url: Optional[str] = None,
    ) -> None:
        app_id = pipeline.app_id
        sha = pipeline.params.get("sha")
        git_repo = pipeline.params.get("repo")

        if (
            not all([app_id, sha, git_repo])
            or not isinstance(sha, str)
            or not isinstance(git_repo, str)
        ):
            logger.info(
                "Missing required params for GitHub status update",
                pipeline_id=str(pipeline.id),
                has_app_id=bool(app_id),
                has_sha=bool(sha),
                has_git_repo=bool(git_repo),
            )
            return

        match status:
            case "success":
                description = "Build succeeded"
                github_state = "success"
            case "committed":
                description = "Build ready"
                github_state = "success"
            case "failure":
                description = "Build failed"
                github_state = "failure"
            case "cancelled":
                description = "Build cancelled"
                github_state = "failure"
            case _:
                description = f"Build status: {status}."
                github_state = "failure"

        target_url = log_url or pipeline.log_url or ""
        if not target_url:
            logger.warning(
                "log_url is unexpectedly None when setting final commit status",
                pipeline_id=str(pipeline.id),
            )

        await update_commit_status(
            sha=sha,
            state=github_state,
            git_repo=git_repo,
            description=description,
            target_url=target_url,
        )

    async def notify_build_started(
        self,
        pipeline: Pipeline,
        log_url: str,
    ) -> None:
        sha = pipeline.params.get("sha")
        git_repo = pipeline.params.get("repo")

        if (
            not all([sha, git_repo])
            or not isinstance(sha, str)
            or not isinstance(git_repo, str)
        ):
            logger.info(
                "Missing required params for GitHub status update",
                pipeline_id=str(pipeline.id),
                has_sha=bool(sha),
                has_git_repo=bool(git_repo),
            )
            return

        await update_commit_status(
            sha=sha,
            state="pending",
            git_repo=git_repo,
            description="Build in progress",
            target_url=log_url,
        )

    async def notify_pr_build_started(
        self,
        pipeline: Pipeline,
        log_url: str,
    ) -> None:
        pr_number_str = pipeline.params.get("pr_number")
        git_repo = pipeline.params.get("repo")

        if not pr_number_str or not git_repo:
            logger.error(
                "Missing required params for PR comment",
                pipeline_id=str(pipeline.id),
                has_pr_number=bool(pr_number_str),
                has_git_repo=bool(git_repo),
            )
            return

        try:
            pr_number = int(pr_number_str)
            comment = f"🚧 Started [test build]({log_url})."
            await create_pr_comment(
                git_repo=git_repo,
                pr_number=pr_number,
                comment=comment,
            )
        except ValueError:
            logger.error(
                "Invalid PR number. Skipping PR comment",
                pr_number=pr_number_str,
                pipeline_id=str(pipeline.id),
            )
        except Exception as e:
            logger.error(
                "Error creating 'Started' PR comment",
                pipeline_id=str(pipeline.id),
                error=str(e),
            )

    async def notify_pr_build_complete(
        self,
        pipeline: Pipeline,
        status: str,
    ) -> None:
        pr_number_str = pipeline.params.get("pr_number")
        git_repo = pipeline.params.get("repo")

        if not pr_number_str or not git_repo:
            logger.error(
                "Missing required params for PR comment",
                pipeline_id=str(pipeline.id),
                has_pr_number=bool(pr_number_str),
                has_git_repo=bool(git_repo),
            )
            return

        try:
            pr_number = int(pr_number_str)
            log_url = pipeline.log_url
            comment = ""

            if status == "committed":
                if pipeline.build_id and self.flat_manager:
                    download_url = self.flat_manager.get_flatpakref_url(
                        pipeline.build_id, pipeline.app_id
                    )
                    comment = f"✅ [Test build succeeded]({log_url}). To test this build, install it from the testing repository:\n\n```\nflatpak install --user {download_url}\n```"
                else:
                    comment = f"✅ [Test build succeeded]({log_url})."
            elif status == "failure":
                comment = f"❌ [Test build]({log_url}) failed."
            elif status == "cancelled":
                comment = f"❌ [Test build]({log_url}) was cancelled."

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
                pipeline_id=str(pipeline.id),
            )
        except Exception as e:
            logger.error(
                "Error creating final PR comment",
                pipeline_id=str(pipeline.id),
                error=str(e),
            )

    async def create_stable_build_failure_issue(
        self,
        pipeline: Pipeline,
    ) -> None:
        if pipeline.flat_manager_repo != "stable":
            return

        git_repo = pipeline.params.get("repo")
        if not git_repo:
            logger.error(
                "Missing git_repo in params. Cannot create issue for failed stable build",
                pipeline_id=str(pipeline.id),
            )
            return

        try:
            app_id = pipeline.app_id
            sha = pipeline.params.get("sha")
            log_url = pipeline.log_url

            title = "Stable build failed"
            body = f"The stable build pipeline for `{app_id}` failed.\n\nCommit SHA: {sha}\n"

            if log_url:
                body += f"Build log: {log_url}"
            else:
                body += "Build log URL not available."

            if log_url:
                body += "\n\nYou can retry the build by commenting `bot, retry` in this issue."

            body += "\n\ncc @flathub/build-moderation"

            issue_url = await create_github_issue(
                git_repo=git_repo,
                title=title,
                body=body,
            )

            if issue_url:
                logger.info(
                    "Successfully created GitHub issue",
                    pipeline_id=str(pipeline.id),
                    issue_url=issue_url,
                )
        except Exception as e:
            logger.error(
                "Failed to create GitHub issue for failed stable build",
                pipeline_id=str(pipeline.id),
                error=str(e),
            )

    async def create_stable_job_failure_issue(
        self,
        pipeline: Pipeline,
        job_type: str,
        job_id: int,
        job_response: Optional[dict] = None,
    ) -> None:
        if pipeline.flat_manager_repo not in ["stable", "beta"]:
            return

        git_repo = pipeline.params.get("repo")
        if not git_repo:
            logger.error(
                "Missing git_repo in params. Cannot create issue for failed job",
                pipeline_id=str(pipeline.id),
                job_type=job_type,
            )
            return

        try:
            app_id = pipeline.app_id
            sha = pipeline.params.get("sha")
            repo = pipeline.flat_manager_repo.capitalize()

            job_type_display = {
                "commit": "commit",
                "publish": "publish",
                "update-repo": "repository update",
            }.get(job_type, job_type)

            title = f"{repo} {job_type_display} job failed for {app_id}"

            body = f"The {job_type} job for `{app_id}` failed in the {pipeline.flat_manager_repo} repository.\n\n"
            body += "**Build Information:**\n"
            body += f"- Commit SHA: {sha}\n"

            if pipeline.build_id:
                body += f"- Build ID: {pipeline.build_id}\n"

            if pipeline.log_url:
                body += f"- Build log: {pipeline.log_url}\n"

            body += "\n**Job Details:**\n"
            body += f"- Job ID: {job_id}\n"
            body += f"- Job status: {settings.flat_manager_url}/status/{job_id}\n"

            if job_response and job_response.get("log"):
                log_content = job_response["log"]
                log_lines = log_content.strip().split("\n")

                if len(log_lines) > 25:
                    relevant_lines = log_lines[-25:]
                    body += "\n**Error Details:**\n```\n"
                    body += "...\n" + "\n".join(relevant_lines) + "\n```\n"
                else:
                    body += "\n**Error Details:**\n```\n"
                    body += log_content + "\n```\n"

            body += "\ncc @flathub/build-moderation"

            issue_url = await create_github_issue(
                git_repo=git_repo,
                title=title,
                body=body,
            )

            if issue_url:
                logger.info(
                    "Successfully created GitHub issue",
                    pipeline_id=str(pipeline.id),
                    issue_url=issue_url,
                )
        except Exception as e:
            logger.error(
                "Failed to create GitHub issue for failed job",
                pipeline_id=str(pipeline.id),
                job_type=job_type,
                job_id=job_id,
                error=str(e),
            )

    async def handle_build_completion(
        self,
        pipeline: Pipeline,
        status: str,
        flat_manager_client: Optional[FlatManagerClient] = None,
    ) -> None:
        if flat_manager_client:
            self.flat_manager = flat_manager_client

        await self.notify_build_status(pipeline, status)

        if status == "failure":
            await self.create_stable_build_failure_issue(pipeline)

        if pipeline.params.get("pr_number") and status != "success":
            await self.notify_pr_build_complete(pipeline, status)

    async def handle_build_started(
        self,
        pipeline: Pipeline,
        log_url: str,
    ) -> None:
        await self.notify_build_started(pipeline, log_url)

        if pipeline.params.get("pr_number"):
            await self.notify_pr_build_started(pipeline, log_url)

    async def handle_build_committed(
        self,
        pipeline: Pipeline,
        flat_manager_client: Optional[FlatManagerClient] = None,
    ) -> None:
        if flat_manager_client:
            self.flat_manager = flat_manager_client

        await self.notify_build_status(pipeline, "committed")

        if pipeline.params.get("pr_number"):
            await self.notify_pr_build_complete(pipeline, "committed")

    async def notify_flat_manager_job_status(
        self,
        pipeline: Pipeline,
        job_type: str,
        job_id: int,
        status: str,
        description: str,
    ) -> None:
        sha = pipeline.params.get("sha")
        git_repo = pipeline.params.get("repo")

        if (
            not all([sha, git_repo])
            or not isinstance(sha, str)
            or not isinstance(git_repo, str)
        ):
            logger.warning(
                "Missing required params for flat-manager job status update",
                pipeline_id=str(pipeline.id),
                has_sha=bool(sha),
                has_git_repo=bool(git_repo),
            )
            return

        context = f"flat-manager/{job_type}"
        target_url = f"{settings.flat_manager_url}/status/{job_id}"

        await update_commit_status(
            sha=sha,
            state=status,
            git_repo=git_repo,
            description=description,
            target_url=target_url,
            context=context,
        )
