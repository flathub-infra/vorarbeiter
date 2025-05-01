import structlog

import httpx

from app.config import settings

logger = structlog.get_logger(__name__)


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
                "Successfully updated GitHub status",
                git_repo=git_repo,
                commit=sha,
                state=state,
            )
    except httpx.RequestError as e:
        logger.error(
            "Request error updating GitHub status",
            git_repo=git_repo,
            commit=sha,
            error=str(e),
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error updating GitHub status",
            git_repo=git_repo,
            commit=sha,
            status_code=e.response.status_code,
            response_text=e.response.text,
        )
    except Exception as e:
        logger.error(
            "Unexpected error updating GitHub status",
            git_repo=git_repo,
            commit=sha,
            error=str(e),
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
                "Successfully created PR comment",
                git_repo=git_repo,
                pr_number=pr_number,
            )
    except httpx.RequestError as e:
        logger.error(
            "Request error creating PR comment",
            git_repo=git_repo,
            pr_number=pr_number,
            error=str(e),
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error creating PR comment",
            git_repo=git_repo,
            pr_number=pr_number,
            status_code=e.response.status_code,
            response_text=e.response.text,
        )
    except Exception as e:
        logger.error(
            "Unexpected error creating PR comment",
            git_repo=git_repo,
            pr_number=pr_number,
            error=str(e),
        )


async def create_github_issue(git_repo: str, title: str, body: str) -> None:
    if not git_repo:
        logger.error("Missing git_repo for GitHub issue. Skipping issue creation.")
        return

    url = f"https://api.github.com/repos/{git_repo}/issues"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {settings.github_status_token}",
    }
    payload = {"title": title, "body": body}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
            issue_url = response.json().get("html_url", "unknown URL")
            logger.info(
                "Successfully created GitHub issue",
                git_repo=git_repo,
                issue_url=issue_url,
            )
    except httpx.RequestError as e:
        logger.error(
            "Request error creating GitHub issue",
            git_repo=git_repo,
            error=str(e),
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error creating GitHub issue",
            git_repo=git_repo,
            status_code=e.response.status_code,
            response_text=e.response.text,
        )
    except Exception as e:
        logger.error(
            "Unexpected error creating GitHub issue",
            git_repo=git_repo,
            error=str(e),
        )
