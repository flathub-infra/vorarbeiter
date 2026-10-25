import json

import structlog

from app.schemas.invite import parse_invite_command
from app.utils.github import (
    add_issue_comment,
    get_github_client,
    is_reviewer_or_admin,
)
from app.utils.manifest import detect_appid_from_github

logger = structlog.get_logger(__name__)

FLATHUB_REPO = "flathub/flathub"
USAGE = "Usage: `/invite repo=<app id> @user1 @user2`"


class InviteService:
    async def handle_invite_command(self, payload: dict) -> None:
        comment_body = payload.get("comment", {}).get("body", "")
        issue_number = payload.get("issue", {}).get("number")
        comment_author = payload.get("comment", {}).get("user", {}).get("login", "")

        try:
            await self._process_invite(comment_body, issue_number, comment_author)
        except Exception:
            logger.exception(
                "Invite command failed",
                issue_number=issue_number,
                comment_author=comment_author,
            )
            if issue_number:
                await self._post_comment(
                    issue_number,
                    "❌ Invite failed due to an unexpected error. Please check the logs.",
                )

    async def _process_invite(
        self, comment_body: str, issue_number: int | None, comment_author: str
    ) -> None:
        if not issue_number:
            logger.error("Missing issue number in invite command payload")
            return

        if not await is_reviewer_or_admin(comment_author):
            logger.warning(
                "Unauthorized invite attempt",
                issue_number=issue_number,
                comment_author=comment_author,
            )
            return

        command = parse_invite_command(comment_body)
        if not command:
            await self._post_comment(
                issue_number, f"❌ Invalid `/invite` command format.\n{USAGE}"
            )
            return

        if comment_author.lower() in (user.lower() for user in command.users):
            await self._post_comment(
                issue_number,
                "❌ You cannot invite yourself. Another reviewer must run `/invite`.",
            )
            return

        repo = f"flathub/{command.repo}"
        client = get_github_client()
        response = await client.request(
            "get",
            f"https://api.github.com/repos/{repo}",
            context={"repo": repo},
            raise_for_status=False,
        )
        if not response or response.status_code != 200:
            await self._post_comment(
                issue_number, f"❌ Repository `{repo}` does not exist."
            )
            return

        repo_data = response.json()
        if repo_data.get("archived"):
            await self._post_comment(
                issue_number, f"❌ Repository `{repo}` is archived."
            )
            return

        _, appid = await detect_appid_from_github(
            client, repo, repo_data.get("default_branch", "master")
        )
        if appid != command.repo:
            await self._post_comment(
                issue_number, f"❌ Repository `{repo}` is not an app repository."
            )
            return

        results = []
        for user in command.users:
            logger.info(
                "Inviting collaborator",
                repo=repo,
                user=user,
                comment_author=comment_author,
            )
            response = await client.request(
                "put",
                f"https://api.github.com/repos/{repo}/collaborators/{user}",
                content=json.dumps({"permission": "push"}),
                context={"repo": repo, "user": user},
                raise_for_status=False,
            )
            status = response.status_code if response else None
            if status == 201:
                results.append(f"- ✅ @{user}: invitation sent")
            elif status == 204:
                results.append(f"- ℹ️ @{user}: already has access")
            else:
                logger.error(
                    "Failed to invite collaborator", repo=repo, user=user, status=status
                )
                results.append(f"- ❌ @{user}: invitation failed")

        await self._post_comment(
            issue_number,
            f"Invitations to `{repo}` (push access):\n\n"
            + "\n".join(results)
            + "\n\ncc @flathub/reviewers",
        )

    async def _post_comment(self, issue_number: int, comment: str) -> None:
        await add_issue_comment(
            git_repo=FLATHUB_REPO, issue_number=issue_number, comment=comment
        )
