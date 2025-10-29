import asyncio
import structlog

import httpx

from typing import Any, Callable

from app.config import settings
from gql import gql, Client
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.exceptions import (
    TransportQueryError,
    TransportServerError,
    TransportProtocolError,
    TransportError,
    TransportClosed,
    TransportAlreadyConnected,
)

GQL_EXCEPTIONS = (
    TransportQueryError,
    TransportServerError,
    TransportProtocolError,
    TransportError,
    TransportClosed,
    TransportAlreadyConnected,
)


logger = structlog.get_logger(__name__)


async def update_commit_status(
    sha: str,
    state: str,
    git_repo: str,
    target_url: str | None = None,
    description: str | None = None,
    context: str = "builds/x86_64",
) -> None:
    if not git_repo:
        logger.error(
            "Missing git_repo for GitHub status update. Skipping status update."
        )
        return

    if not sha:
        logger.error("Missing commit SHA. Skipping status update.")
        return

    if sha == "0000000000000000000000000000000000000000":
        logger.warning(
            "Detected null SHA (branch deletion). Skipping status update.",
            git_repo=git_repo,
            sha=sha,
        )
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

    max_retries = 3
    retry_count = 0
    base_delay = 1.0

    while retry_count <= max_retries:
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
                return
        except httpx.RequestError as e:
            if retry_count < max_retries:
                delay = base_delay * (2**retry_count)
                logger.warning(
                    "Request error updating GitHub status, retrying after delay",
                    git_repo=git_repo,
                    commit=sha,
                    error=str(e),
                    retry_count=retry_count + 1,
                    max_retries=max_retries,
                    delay_seconds=delay,
                )
                retry_count += 1
                await asyncio.sleep(delay)
                continue
            else:
                logger.error(
                    "Request error updating GitHub status",
                    git_repo=git_repo,
                    commit=sha,
                    error=str(e),
                    retry_count=retry_count,
                    max_retries=max_retries,
                )
                return
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 500 and retry_count < max_retries:
                delay = base_delay * (2**retry_count)
                logger.warning(
                    "GitHub API returned 500, retrying after delay",
                    git_repo=git_repo,
                    commit=sha,
                    status_code=e.response.status_code,
                    retry_count=retry_count + 1,
                    max_retries=max_retries,
                    delay_seconds=delay,
                )
                retry_count += 1
                await asyncio.sleep(delay)
                continue
            else:
                logger.error(
                    "HTTP error updating GitHub status",
                    git_repo=git_repo,
                    commit=sha,
                    status_code=e.response.status_code,
                    response_text=e.response.text,
                    retry_count=retry_count,
                    max_retries=max_retries,
                )
                return
        except Exception as e:
            logger.error(
                "Unexpected error updating GitHub status",
                git_repo=git_repo,
                commit=sha,
                error=str(e),
            )
            return


async def create_pr_comment(git_repo: str, pr_number: int, comment: str) -> None:
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


async def create_github_issue(git_repo: str, title: str, body: str) -> str | None:
    if not git_repo:
        logger.error("Missing git_repo for GitHub issue. Skipping issue creation.")
        return None

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
            issue_data = response.json()
            issue_url = issue_data.get("html_url", "unknown URL")
            logger.info(
                "Successfully created GitHub issue",
                git_repo=git_repo,
                issue_url=issue_url,
            )
            return issue_url
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
    return None


async def close_github_issue(git_repo: str, issue_number: int) -> bool:
    if not git_repo:
        logger.error("Missing git_repo for GitHub issue. Skipping issue closure.")
        return False

    if not issue_number:
        logger.error("Missing issue number. Skipping issue closure.")
        return False

    url = f"https://api.github.com/repos/{git_repo}/issues/{issue_number}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {settings.github_status_token}",
    }
    payload = {"state": "closed"}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                url,
                headers=headers,
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
            logger.info(
                "Successfully closed GitHub issue",
                git_repo=git_repo,
                issue_number=issue_number,
            )
            return True
    except httpx.RequestError as e:
        logger.error(
            "Request error closing GitHub issue",
            git_repo=git_repo,
            issue_number=issue_number,
            error=str(e),
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error closing GitHub issue",
            git_repo=git_repo,
            issue_number=issue_number,
            status_code=e.response.status_code,
            response_text=e.response.text,
        )
    except Exception as e:
        logger.error(
            "Unexpected error closing GitHub issue",
            git_repo=git_repo,
            issue_number=issue_number,
            error=str(e),
        )
    return False


async def add_issue_comment(
    git_repo: str, issue_number: int, comment: str, check_duplicates: bool = False
) -> bool:
    if not git_repo:
        logger.error("Missing git_repo for GitHub issue comment. Skipping comment.")
        return False
    if not issue_number:
        logger.error("Missing issue number. Skipping comment.")
        return False

    url = f"https://api.github.com/repos/{git_repo}/issues/{issue_number}/comments"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {settings.github_status_token}",
    }
    payload = {"body": comment}

    try:
        async with httpx.AsyncClient() as client:
            if check_duplicates:
                comments_response = await client.get(
                    url,
                    headers=headers,
                    timeout=10.0,
                )
                comments_response.raise_for_status()
                existing_comments = comments_response.json()
                for existing_comment in existing_comments:
                    if comment in existing_comment.get("body", ""):
                        logger.info(
                            "Comment with same body already exists on GitHub issue. Skipping.",
                            git_repo=git_repo,
                            issue_number=issue_number,
                        )
                        return True

            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
            logger.info(
                "Successfully added comment to GitHub issue",
                git_repo=git_repo,
                issue_number=issue_number,
            )
            return True
    except httpx.RequestError as e:
        logger.error(
            "Request error adding comment to GitHub issue",
            git_repo=git_repo,
            issue_number=issue_number,
            error=str(e),
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error adding comment to GitHub issue",
            git_repo=git_repo,
            issue_number=issue_number,
            status_code=e.response.status_code,
            response_text=e.response.text,
        )
    except Exception as e:
        logger.error(
            "Unexpected error adding comment to GitHub issue",
            git_repo=git_repo,
            issue_number=issue_number,
            error=str(e),
        )
    return False


async def is_issue_edited(git_repo: str, issue_number: int) -> bool | None:
    if not git_repo or "/" not in git_repo:
        logger.error("Invalid git_repo format. Expected 'owner/repo'.")
        return None

    owner, name = git_repo.split("/", 1)

    transport = RequestsHTTPTransport(
        url="https://api.github.com/graphql",
        headers={"Authorization": f"Bearer {settings.github_status_token}"},
    )
    client = Client(transport=transport, fetch_schema_from_transport=False)

    gql_check_issue_edited = gql(
        """
        query ($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            issue(number: $number) {
              createdAt
              lastEditedAt
            }
          }
        }
        """
    )

    try:
        data = client.execute(
            gql_check_issue_edited,
            variable_values={"owner": owner, "name": name, "number": issue_number},
        )

        issue_data = data.get("repository", {}).get("issue")
        if not issue_data:
            logger.error(
                "Issue not found in GraphQL response",
                git_repo=git_repo,
                issue_number=issue_number,
            )
            return None

        created_at = issue_data.get("createdAt")
        last_edited_at = issue_data.get("lastEditedAt")

        if last_edited_at is None:
            logger.info(
                "Issue was not edited",
                git_repo=git_repo,
                issue_number=issue_number,
                created_at=created_at,
            )
            return False

        if created_at and last_edited_at and created_at != last_edited_at:
            logger.info(
                "Issue was edited",
                git_repo=git_repo,
                issue_number=issue_number,
                created_at=created_at,
                last_edited_at=last_edited_at,
            )
            return True

        logger.info(
            "Issue was not edited",
            git_repo=git_repo,
            issue_number=issue_number,
            created_at=created_at,
        )
        return False

    except GQL_EXCEPTIONS as err:
        logger.error(
            "GraphQL exception while checking issue edit status",
            git_repo=git_repo,
            issue_number=issue_number,
            error=str(err),
            exc_info=True,
        )
        return None
    except Exception as e:
        logger.error(
            "Unexpected error checking issue edit status",
            git_repo=git_repo,
            issue_number=issue_number,
            error=str(e),
            exc_info=True,
        )
        return None


async def get_workflow_run_title(run_id: int) -> str | None:
    repo = "flathub-infra/vorarbeiter"
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {settings.github_status_token}",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()
            run_data = response.json()
            title = run_data.get("display_title", "") or run_data.get("name", "")

            logger.info(
                "Successfully fetched workflow run title",
                run_id=run_id,
                title=title,
            )
            return title
    except httpx.RequestError as e:
        logger.error(
            "Request error fetching workflow run title",
            run_id=run_id,
            error=str(e),
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error fetching workflow run title",
            run_id=run_id,
            status_code=e.response.status_code,
            response_text=e.response.text,
        )
    except Exception as e:
        logger.error(
            "Unexpected error fetching workflow run title",
            run_id=run_id,
            error=str(e),
        )
    return None


async def get_check_run_annotations(
    owner: str,
    repo: str,
    run_id: int,
    job_filter: Callable[[dict], bool] | None = None,
) -> list[dict] | None:
    """Get annotations from all check-runs for a workflow run.

    Returns a list of annotation dicts with 'message' and 'annotation_level' keys,
    or None if there was an error.
    """
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {settings.github_status_token}",
    }

    annotations: list[dict[str | None, str | None]] = []

    try:
        async with httpx.AsyncClient() as client:
            jobs_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
            response = await client.get(
                jobs_url,
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()
            jobs = response.json().get("jobs", [])

            for job in jobs:
                if job_filter and not job_filter(job):
                    continue

                if check_run_url := job.get("check_run_url"):
                    try:
                        annotations_response = await client.get(
                            f"{check_run_url}/annotations",
                            headers=headers,
                            timeout=10.0,
                        )
                        if annotations_response.status_code == 200:
                            annotations.extend(
                                {
                                    "message": a.get("message"),
                                    "annotation_level": a.get("annotation_level"),
                                }
                                for a in annotations_response.json()
                            )
                    except httpx.HTTPError as e:
                        logger.warning(
                            "Failed to fetch annotations for job",
                            job_id=job.get("id"),
                            owner=owner,
                            repo=repo,
                            run_id=run_id,
                            error=str(e),
                        )
                        continue

            logger.info(
                "Successfully fetched check-run annotations",
                owner=owner,
                repo=repo,
                run_id=run_id,
                annotation_count=len(annotations),
            )
            return annotations

    except httpx.RequestError as e:
        logger.error(
            "Request error fetching jobs",
            owner=owner,
            repo=repo,
            run_id=run_id,
            error=str(e),
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error fetching jobs",
            owner=owner,
            repo=repo,
            run_id=run_id,
            status_code=e.response.status_code,
            response_text=e.response.text,
        )
    except Exception as e:
        logger.error(
            "Unexpected error fetching check-run annotations",
            owner=owner,
            repo=repo,
            run_id=run_id,
            error=str(e),
        )
    return None


async def get_linter_warning_messages(
    run_id: int, owner: str = "flathub-infra", repo: str = "vorarbeiter"
) -> list[str]:
    def job_filter(job: dict[str, Any]) -> bool:
        return job.get("name", "").startswith(("validate-manifest", "build-"))

    messages: list[str] = []

    annotations = await get_check_run_annotations(
        owner, repo, run_id, job_filter=job_filter
    )
    if annotations is None:
        return messages

    messages = [
        a.get("message", "")
        for a in annotations
        if a.get("message") and "warning found in linter" in a.get("message", "")
    ]
    return list(set(messages))
