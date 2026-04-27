import asyncio
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import get_db
from app.models.merge_request import (
    ACTIVE_MERGE_STATUS_VALUES,
    MergeRequest,
    MergeStatus,
)
from app.schemas.merge import parse_merge_command
from app.utils.github import (
    GQL_EXCEPTIONS,
    GitHubAPIClient,
    get_github_client_for_token,
)
from app.utils.manifest import detect_appid_from_github

logger = structlog.get_logger(__name__)

FLATHUB_REPO = "flathub/flathub"
MERGE_WORKFLOW_REPO = "flathub-infra/vorarbeiter"
MERGE_ERROR_REPO_CREATE_FAILED = "Failed to create repository"
MERGE_ERROR_WORKFLOW_DISPATCH_FAILED = "Failed to dispatch merge workflow"
MERGE_ERROR_GIT_PUSH_FAILED = "Git push workflow failed"

PROTECTED_BRANCH_PATTERNS = (
    "master",
    "main",
    "stable",
    "branch/*",
    "beta",
    "beta/*",
)

CLOSE_COMMENT_TEMPLATE = (
    "A repository for this submission has been created: {repo_url} "
    "and it will be published to Flathub within a few hours.\n"
    "You will receive an [invite]({repo_url}/invitations) "
    "to be a collaborator on the repository. Please make sure to enable "
    "2FA on GitHub and accept the invite within a week.\n"
    "Please go through the [App maintenance guide]"
    "(https://docs.flathub.org/docs/for-app-authors/maintenance) "
    "if you have never maintained an app on Flathub before.\n"
    "If you are the original developer (or an authorized party), please "
    "[verify your app](https://docs.flathub.org/docs/for-app-authors/verification) "
    "to let users know it's coming from you.\n"
    "Please follow the [Flathub blog](https://docs.flathub.org/blog) for the latest "
    "announcements.\n"
    "Thanks!"
)

GQL_GET_REPO_ID = gql(
    """
    query get_repo_id($repo: String!) {
        repository(name: $repo, owner: "flathub") {
            id
        }
    }
    """
)

GQL_ADD_BRANCH_PROTECTION = gql(
    """
    mutation add_branch_protection($repositoryID: ID!, $pattern: String!) {
        createBranchProtectionRule(
            input: {
                allowsDeletions: false
                allowsForcePushes: false
                dismissesStaleReviews: false
                isAdminEnforced: false
                pattern: $pattern
                repositoryId: $repositoryID
                requiresApprovingReviews: true
                requiredApprovingReviewCount: 0
                requiresCodeOwnerReviews: false
                requiresStatusChecks: true
                requiresStrictStatusChecks: true
                restrictsReviewDismissals: false
                requiredStatusCheckContexts: ["builds/x86_64"]
            }
        ) {
            branchProtectionRule {
                id
            }
        }
    }
    """
)


class MergeCallbackError(ValueError):
    pass


class MergeNotFoundError(MergeCallbackError):
    pass


class MergeInvalidTokenError(MergeCallbackError):
    pass


class MergeCallbackConflictError(MergeCallbackError):
    pass


class MergeService:
    def _get_client(self) -> GitHubAPIClient:
        return get_github_client_for_token(settings.flathubbot_token)

    async def handle_merge_command(self, payload: dict) -> None:
        comment_body = payload.get("comment", {}).get("body", "")
        pr_number = payload.get("issue", {}).get("number")
        comment_author = payload.get("comment", {}).get("user", {}).get("login", "")

        try:
            await self._process_merge(comment_body, pr_number, comment_author)
        except Exception:
            logger.exception(
                "Merge command failed",
                pr_number=pr_number,
                comment_author=comment_author,
            )
            if pr_number:
                await self._post_comment(
                    pr_number,
                    "❌ Merge failed due to an unexpected error. Please check the logs.",
                )

    async def _process_merge(
        self,
        comment_body: str,
        pr_number: int | None,
        comment_author: str,
    ) -> None:
        if not pr_number:
            logger.error("Missing PR number in merge command payload")
            return

        command = parse_merge_command(comment_body)
        if not command:
            await self._post_comment(
                pr_number,
                "❌ Invalid `/merge` command format.\n"
                "Usage: `/merge:<optional branch, default: master> "
                "head=<pr head commit sha 40 chars> "
                "<optional extra collaborators @foo @baz>`",
            )
            return

        logger.info(
            "Processing merge command",
            pr_number=pr_number,
            target_branch=command.target_branch,
            pr_head_sha=command.pr_head_sha,
            comment_author=comment_author,
        )

        if not await self._is_authorized(comment_author):
            logger.warning(
                "Unauthorized merge attempt",
                pr_number=pr_number,
                comment_author=comment_author,
            )
            return

        pr_details = await self._get_pr_details(pr_number)
        if not pr_details:
            await self._post_comment(pr_number, "❌ Failed to fetch PR details.")
            return

        if pr_details["state"] != "open":
            await self._post_comment(pr_number, "❌ Cannot merge: PR is not open.")
            return

        current_sha = pr_details["head_sha"]
        if current_sha != command.pr_head_sha:
            await self._post_comment(
                pr_number,
                f"❌ SHA mismatch: PR HEAD is `{current_sha}` "
                f"but command specifies `{command.pr_head_sha}`.",
            )
            return

        fork_repo = pr_details["fork_repo"]
        fork_branch = pr_details["fork_branch"]
        fork_url = pr_details["fork_clone_url"]
        pr_author = pr_details["author"]

        client = self._get_client()
        manifest_file, appid = await detect_appid_from_github(
            client, fork_repo, fork_branch
        )
        if not manifest_file or not appid:
            await self._post_comment(
                pr_number, "❌ Failed to detect app ID from manifest."
            )
            return

        logger.info("Detected app ID", appid=appid, manifest_file=manifest_file)

        if await self._has_active_merge_request(pr_number):
            await self._post_comment(
                pr_number,
                "❌ A merge operation is already in progress for this PR.",
            )
            return

        retry_repo_url: str | None = None
        repo_exists = await self._check_repo_exists(appid)
        if repo_exists:
            retry_repo_url = await self._get_retryable_repo_url(pr_number, appid)
            if not retry_repo_url:
                await self._post_comment(
                    pr_number,
                    f"❌ Repository `flathub/{appid}` already exists.",
                )
                return

        collaborators = list(command.additional_collaborators)
        collaborators.append(pr_author)

        merge_request = MergeRequest(
            id=uuid.uuid4(),
            pr_number=pr_number,
            app_id=appid,
            target_branch=command.target_branch,
            pr_head_sha=command.pr_head_sha,
            collaborators=collaborators,
            status=MergeStatus.PUSHING,
            comment_author=comment_author,
            fork_url=fork_url,
            fork_branch=fork_branch,
            repo_html_url=retry_repo_url,
        )

        try:
            async with get_db() as db:
                db.add(merge_request)
                await db.flush()
        except IntegrityError:
            await self._post_comment(
                pr_number,
                "❌ A merge operation is already in progress for this PR.",
            )
            return

        if not repo_exists:
            repo_html_url = await self._create_repo(appid)
            if not repo_html_url:
                await self._mark_failed(
                    merge_request.id,
                    MERGE_ERROR_REPO_CREATE_FAILED,
                )
                await self._post_comment(
                    pr_number, f"❌ Failed to create repository `flathub/{appid}`."
                )
                return

            merge_request.repo_html_url = repo_html_url

        try:
            await self._dispatch_merge_workflow(merge_request)
        except Exception:
            logger.exception(
                "Failed to dispatch merge workflow",
                merge_id=str(merge_request.id),
            )
            await self._mark_failed(
                merge_request.id,
                MERGE_ERROR_WORKFLOW_DISPATCH_FAILED,
                repo_html_url=merge_request.repo_html_url,
            )
            await self._post_comment(pr_number, "❌ Failed to dispatch merge workflow.")
            return

        logger.info(
            "Merge workflow dispatched",
            merge_id=str(merge_request.id),
            appid=appid,
        )

    async def handle_callback(
        self, merge_id: uuid.UUID, callback_token: str, status: str
    ) -> None:
        async with get_db() as db:
            merge_request = await db.get(MergeRequest, merge_id)
            if not merge_request:
                logger.error("Merge request not found", merge_id=str(merge_id))
                raise MergeNotFoundError(f"Merge request {merge_id} not found")

            if not secrets.compare_digest(callback_token, merge_request.callback_token):
                logger.error("Invalid callback token", merge_id=str(merge_id))
                raise MergeInvalidTokenError("Invalid callback token")

            if merge_request.status != MergeStatus.PUSHING:
                logger.warning(
                    "Unexpected merge request status for callback",
                    merge_id=str(merge_id),
                    status=merge_request.status.value,
                )
                raise MergeCallbackConflictError(
                    f"Merge request is in state {merge_request.status.value}"
                )

        if status != "success":
            await self._mark_failed(merge_id, MERGE_ERROR_GIT_PUSH_FAILED)
            await self._post_comment(
                merge_request.pr_number,
                "❌ Merge failed: git push workflow reported failure.",
            )
            return

        await self._finalize(merge_request)

    async def _finalize(self, merge_request: MergeRequest) -> None:
        merge_id = merge_request.id
        appid = merge_request.app_id
        pr_number = merge_request.pr_number
        errors: list[str] = []

        async with get_db() as db:
            mr = await db.get(MergeRequest, merge_id)
            if mr:
                mr.status = MergeStatus.FINALIZING
                await db.commit()

        logger.info("Finalizing merge", merge_id=str(merge_id), appid=appid)

        if not await self._remove_collaborator(appid, "flathubbot"):
            errors.append("Failed to remove flathubbot from collaborators")

        if not await self._set_all_branch_protections(appid, PROTECTED_BRANCH_PATTERNS):
            errors.append("Failed to set one or more branch protections")

        if not await self._add_collaborators(appid, merge_request.collaborators):
            errors.append("Failed to add one or more collaborators")

        sha_ok, protected = await self._verify_branch_state(
            appid, merge_request.target_branch, merge_request.pr_head_sha
        )
        if not sha_ok:
            errors.append("Remote HEAD SHA verification failed")
        if not protected:
            errors.append("Branch protection verification failed")

        if not await self._set_labels(pr_number):
            errors.append("Failed to set labels")

        repo_url = merge_request.repo_html_url or f"https://github.com/flathub/{appid}"
        if not await self._close_and_lock_pr(pr_number, repo_url):
            errors.append("Failed to close/lock PR")

        error_msg = "; ".join(errors) if errors else None
        if error_msg:
            logger.error(
                "Merge finalization completed with errors",
                merge_id=str(merge_id),
                errors=errors,
            )
        else:
            logger.info("Merge completed successfully", merge_id=str(merge_id))

        async with get_db() as db:
            mr = await db.get(MergeRequest, merge_id)
            if mr:
                mr.status = MergeStatus.COMPLETED
                mr.error = error_msg
                mr.completed_at = datetime.now(timezone.utc)
                await db.commit()

    async def _is_authorized(self, username: str) -> bool:
        client = self._get_client()
        for team in ("admins", "reviewers"):
            response = await client.request(
                "get",
                f"https://api.github.com/orgs/flathub/teams/{team}/members/{username}",
                context={"team": team, "username": username},
                raise_for_status=False,
            )
            if response and response.status_code == 204:
                return True
        return False

    async def _get_pr_details(self, pr_number: int) -> dict[str, Any] | None:
        client = self._get_client()
        response = await client.request(
            "get",
            f"https://api.github.com/repos/{FLATHUB_REPO}/pulls/{pr_number}",
            context={"pr_number": pr_number},
        )
        if not response:
            return None

        data = response.json()
        head = data.get("head", {})
        head_repo = head.get("repo") or {}

        return {
            "state": data.get("state"),
            "head_sha": head.get("sha"),
            "fork_repo": head_repo.get("full_name"),
            "fork_branch": head.get("ref"),
            "fork_clone_url": head_repo.get("clone_url"),
            "author": data.get("user", {}).get("login"),
        }

    async def _check_repo_exists(self, appid: str) -> bool:
        client = self._get_client()
        response = await client.request(
            "get",
            f"https://api.github.com/repos/flathub/{appid}",
            context={"appid": appid},
            raise_for_status=False,
        )
        return response is not None and response.status_code == 200

    async def _has_active_merge_request(self, pr_number: int) -> bool:
        async with get_db() as db:
            result = await db.execute(
                select(MergeRequest.id).where(
                    MergeRequest.pr_number == pr_number,
                    MergeRequest.status.in_(ACTIVE_MERGE_STATUS_VALUES),
                )
            )
            return result.scalar_one_or_none() is not None

    async def _get_retryable_repo_url(self, pr_number: int, appid: str) -> str | None:
        async with get_db() as db:
            result = await db.execute(
                select(MergeRequest)
                .where(
                    MergeRequest.pr_number == pr_number,
                    MergeRequest.app_id == appid,
                )
                .order_by(MergeRequest.created_at.desc())
            )
            latest_request = result.scalars().first()

        if latest_request is None:
            return None

        if (
            latest_request.status != MergeStatus.FAILED
            or latest_request.error != MERGE_ERROR_WORKFLOW_DISPATCH_FAILED
        ):
            return None

        return latest_request.repo_html_url or f"https://github.com/flathub/{appid}"

    async def _create_repo(self, appid: str) -> str | None:
        client = self._get_client()

        response = await client.request(
            "post",
            "https://api.github.com/orgs/flathub/repos",
            content=json.dumps({"name": appid}),
            context={"appid": appid},
        )
        if not response:
            return None

        repo_data = response.json()
        html_url = repo_data.get("html_url")

        edit_response = await client.request(
            "patch",
            f"https://api.github.com/repos/flathub/{appid}",
            content=json.dumps(
                {
                    "homepage": f"https://flathub.org/apps/details/{appid}",
                    "delete_branch_on_merge": True,
                }
            ),
            context={"appid": appid},
        )
        if not edit_response:
            logger.warning("Failed to edit repo settings", appid=appid)

        return html_url

    async def _dispatch_merge_workflow(self, merge_request: MergeRequest) -> None:
        client = self._get_client()
        callback_url = f"{settings.base_url}/api/merge/{merge_request.id}/callback"

        payload = {
            "ref": "main",
            "inputs": {
                "fork_url": merge_request.fork_url,
                "expected_sha": merge_request.pr_head_sha,
                "appid": merge_request.app_id,
                "target_branch": merge_request.target_branch,
                "flathubbot_token": settings.flathubbot_token,
                "callback_url": callback_url,
                "callback_token": merge_request.callback_token,
            },
        }

        response = await client.request(
            "post",
            f"https://api.github.com/repos/{MERGE_WORKFLOW_REPO}/actions/workflows/merge.yml/dispatches",
            content=json.dumps(payload),
            context={"merge_id": str(merge_request.id)},
        )

        if not response:
            raise RuntimeError("Failed to dispatch merge workflow")

    async def _set_all_branch_protections(
        self, appid: str, patterns: tuple[str, ...]
    ) -> bool:
        transport = RequestsHTTPTransport(
            url="https://api.github.com/graphql",
            headers={"Authorization": f"Bearer {settings.flathubbot_token}"},
        )
        client = Client(transport=transport, fetch_schema_from_transport=False)

        try:
            repo_data = await asyncio.to_thread(
                client.execute,
                GQL_GET_REPO_ID,
                variable_values={"repo": appid},
            )
            if not isinstance(repo_data, dict):
                logger.error("Unexpected GraphQL response type", appid=appid)
                return False
            repo_id = repo_data["repository"]["id"]

            success = True
            for pattern in patterns:
                try:
                    await asyncio.to_thread(
                        client.execute,
                        GQL_ADD_BRANCH_PROTECTION,
                        variable_values={"repositoryID": repo_id, "pattern": pattern},
                    )
                except GQL_EXCEPTIONS as err:
                    logger.error(
                        "Failed to set branch protection",
                        appid=appid,
                        pattern=pattern,
                        error=str(err),
                    )
                    success = False
            return success
        except GQL_EXCEPTIONS as err:
            logger.error(
                "Failed to fetch repo ID for branch protection",
                appid=appid,
                error=str(err),
            )
            return False

    async def _add_collaborators(self, appid: str, collaborators: list[str]) -> bool:
        client = self._get_client()
        teams_to_add: list[str] = ["trusted-maintainers"]

        if appid.startswith("org.kde."):
            teams_to_add.append("KDE")
        elif appid.startswith("org.gnome.") and appid.count(".") == 2:
            teams_to_add.append("GNOME")

        users = []
        for collab in collaborators:
            if collab.startswith("flathub/"):
                teams_to_add.append(collab.split("/", 1)[1])
            else:
                users.append(collab)

        success = True
        for user in dict.fromkeys(users):
            logger.info("Adding user collaborator", appid=appid, user=user)
            response = await client.request(
                "put",
                f"https://api.github.com/repos/flathub/{appid}/collaborators/{user}",
                content=json.dumps({"permission": "push"}),
                context={"appid": appid, "user": user},
                raise_for_status=False,
            )
            if not response or response.status_code not in (201, 204):
                logger.error("Failed to add collaborator", appid=appid, user=user)
                success = False

        for team in dict.fromkeys(teams_to_add):
            logger.info("Adding team collaborator", appid=appid, team=team)
            response = await client.request(
                "put",
                f"https://api.github.com/orgs/flathub/teams/{team}/repos/flathub/{appid}",
                content=json.dumps({"permission": "push"}),
                context={"appid": appid, "team": team},
                raise_for_status=False,
            )
            if not response or response.status_code not in (204,):
                logger.error("Failed to add team", appid=appid, team=team)
                success = False

        return success

    async def _remove_collaborator(self, appid: str, username: str) -> bool:
        client = self._get_client()
        response = await client.request(
            "delete",
            f"https://api.github.com/repos/flathub/{appid}/collaborators/{username}",
            context={"appid": appid, "username": username},
            raise_for_status=False,
        )
        return response is not None and response.status_code in (204, 404)

    async def _verify_branch_state(
        self, appid: str, branch: str, expected_sha: str
    ) -> tuple[bool, bool]:
        client = self._get_client()
        response = await client.request(
            "get",
            f"https://api.github.com/repos/flathub/{appid}/branches/{branch}",
            context={"appid": appid, "branch": branch},
            raise_for_status=False,
        )
        if not response or response.status_code != 200:
            return False, False

        data = response.json()
        actual_sha = data.get("commit", {}).get("sha")
        sha_ok = actual_sha == expected_sha
        if not sha_ok:
            logger.error(
                "Remote HEAD SHA mismatch",
                appid=appid,
                branch=branch,
                expected=expected_sha,
                actual=actual_sha,
            )
        return sha_ok, data.get("protected", False) is True

    async def _set_labels(self, pr_number: int) -> bool:
        client = self._get_client()

        response = await client.request(
            "get",
            f"https://api.github.com/repos/{FLATHUB_REPO}/issues/{pr_number}/labels",
            context={"pr_number": pr_number},
            raise_for_status=False,
        )

        current_labels: list[str] = []
        if response and response.status_code == 200:
            current_labels = [label["name"] for label in response.json()]

        if "migrate-app-id" in current_labels:
            add_response = await client.request(
                "post",
                f"https://api.github.com/repos/{FLATHUB_REPO}/issues/{pr_number}/labels",
                content=json.dumps({"labels": ["ready"]}),
                context={"pr_number": pr_number},
                raise_for_status=False,
            )
            return add_response is not None and add_response.status_code == 200
        else:
            set_response = await client.request(
                "put",
                f"https://api.github.com/repos/{FLATHUB_REPO}/issues/{pr_number}/labels",
                content=json.dumps({"labels": ["ready"]}),
                context={"pr_number": pr_number},
                raise_for_status=False,
            )
            return set_response is not None and set_response.status_code == 200

    async def _close_and_lock_pr(self, pr_number: int, repo_url: str) -> bool:
        client = self._get_client()
        comment = CLOSE_COMMENT_TEMPLATE.format(repo_url=repo_url)

        comment_response = await client.request(
            "post",
            f"https://api.github.com/repos/{FLATHUB_REPO}/issues/{pr_number}/comments",
            content=json.dumps({"body": comment}),
            context={"pr_number": pr_number},
            raise_for_status=False,
        )
        if not comment_response or comment_response.status_code != 201:
            logger.error("Failed to post close comment", pr_number=pr_number)
            return False

        close_response = await client.request(
            "patch",
            f"https://api.github.com/repos/{FLATHUB_REPO}/pulls/{pr_number}",
            content=json.dumps({"state": "closed"}),
            context={"pr_number": pr_number},
            raise_for_status=False,
        )
        if not close_response or close_response.status_code != 200:
            logger.error("Failed to close PR", pr_number=pr_number)
            return False

        lock_response = await client.request(
            "put",
            f"https://api.github.com/repos/{FLATHUB_REPO}/issues/{pr_number}/lock",
            content=json.dumps({"lock_reason": "resolved"}),
            context={"pr_number": pr_number},
            raise_for_status=False,
        )
        if not lock_response or lock_response.status_code not in (204, 200):
            logger.warning("Failed to lock PR", pr_number=pr_number)

        return True

    async def _post_comment(self, pr_number: int, comment: str) -> None:
        client = self._get_client()
        await client.request(
            "post",
            f"https://api.github.com/repos/{FLATHUB_REPO}/issues/{pr_number}/comments",
            content=json.dumps({"body": comment}),
            context={"pr_number": pr_number},
            raise_for_status=False,
        )

    async def _mark_failed(
        self,
        merge_id: uuid.UUID,
        error: str,
        repo_html_url: str | None = None,
    ) -> None:
        async with get_db() as db:
            mr = await db.get(MergeRequest, merge_id)
            if mr:
                mr.status = MergeStatus.FAILED
                mr.error = error
                if repo_html_url and not mr.repo_html_url:
                    mr.repo_html_url = repo_html_url
                mr.completed_at = datetime.now(timezone.utc)
                await db.commit()
