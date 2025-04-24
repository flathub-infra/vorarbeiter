import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def update_commit_status(
    sha: str,
    state: str,
    git_repo: str,
    target_url: str | None = None,
    description: str | None = None,
    context: str = "builds/x86_64",
) -> None:
    if not settings.github_status_token:
        logger.warning("GITHUB_STATUS_TOKEN is not set. Skipping status update.")
        return

    if not git_repo:
        logger.error(
            "Missing git_repo for GitHub status update. Skipping status update."
        )
        return

    if not sha:
        logger.error("Missing commit SHA. Skipping status update.")
        return

    if state not in ["error", "failure", "pending", "success"]:
        logger.error(f"Invalid state '{state}'. Skipping status update.")
        return

    url = f"https://api.github.com/repos/{git_repo}/statuses/{sha}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {settings.github_status_token}",
    }
    payload = {
        "state": state,
        "context": context,
    }

    if target_url:
        payload["target_url"] = target_url

    if description:
        payload["description"] = description

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
            logger.info(
                f"Successfully updated status for {git_repo} commit {sha} to {state}"
            )
    except httpx.RequestError as e:
        logger.error(
            f"Request error updating GitHub status for {git_repo} commit {sha}: {e}"
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error updating GitHub status for {git_repo} commit {sha}: "
            f"{e.response.status_code} - {e.response.text}"
        )
    except Exception as e:
        logger.error(
            f"Unexpected error updating GitHub status for {git_repo} commit {sha}: {e}"
        )


async def create_pr_comment(git_repo: str, pr_number: int, comment: str) -> None:
    if not settings.github_status_token:
        logger.warning("GITHUB_STATUS_TOKEN is not set. Skipping PR comment creation.")
        return

    if not git_repo:
        logger.error("Missing git_repo for GitHub PR comment. Skipping PR comment.")
        return

    if not pr_number:
        logger.error("Missing PR number. Skipping PR comment.")
        return

    url = f"https://api.github.com/repos/{git_repo}/issues/{pr_number}/comments"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {settings.github_status_token}",
    }
    payload = {"body": comment}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
            logger.info(
                f"Successfully created comment on PR #{pr_number} in {git_repo}"
            )
    except httpx.RequestError as e:
        logger.error(
            f"Request error creating comment on PR #{pr_number} in {git_repo}: {e}"
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error creating comment on PR #{pr_number} in {git_repo}: "
            f"{e.response.status_code} - {e.response.text}"
        )
    except Exception as e:
        logger.error(
            f"Unexpected error creating comment on PR #{pr_number} in {git_repo}: {e}"
        )
