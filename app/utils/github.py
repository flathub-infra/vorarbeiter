import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import httpx
import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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


@dataclass
class GitHubAPIResult:
    response: httpx.Response | None
    should_queue: bool = False
    error_type: str | None = None
    retry_after: float | None = None


class GitHubAPIClient:
    DEFAULT_TIMEOUT = 10.0
    DEFAULT_MAX_RETRIES = 3

    def __init__(self, token: str):
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {token}",
        }

    def _is_rate_limit_error(self, response: httpx.Response) -> bool:
        if response.status_code != 403:
            return False
        try:
            body = response.json()
            return "rate limit" in body.get("message", "").lower()
        except Exception:
            return False

    def _get_rate_limit_wait_time(self, response: httpx.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass

        reset_timestamp = response.headers.get("X-RateLimit-Reset")
        if reset_timestamp:
            try:
                wait_time = int(reset_timestamp) - time.time()
                return max(wait_time, 0) + 1
            except ValueError:
                pass

        return 60.0

    async def request(
        self,
        method: str,
        url: str,
        context: dict | None = None,
        max_retries: int | None = None,
        raise_for_status: bool = True,
        **kwargs,
    ) -> httpx.Response | None:
        result = await self.request_with_result(
            method, url, context, max_retries, raise_for_status, **kwargs
        )
        return result.response

    async def request_with_result(
        self,
        method: str,
        url: str,
        context: dict | None = None,
        max_retries: int | None = None,
        raise_for_status: bool = True,
        **kwargs,
    ) -> GitHubAPIResult:
        context = context or {}
        kwargs.setdefault("timeout", self.DEFAULT_TIMEOUT)
        retries = max_retries if max_retries is not None else self.DEFAULT_MAX_RETRIES

        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient() as client:
                    response = await getattr(client, method)(
                        url, headers=self.headers, **kwargs
                    )

                    if self._is_rate_limit_error(response):
                        wait_time = self._get_rate_limit_wait_time(response)
                        logger.warning(
                            "Rate limited by GitHub API",
                            url=url,
                            retry_after=wait_time,
                            **context,
                        )
                        return GitHubAPIResult(
                            response=None,
                            should_queue=True,
                            error_type="rate_limit",
                            retry_after=wait_time,
                        )

                    if raise_for_status:
                        response.raise_for_status()
                    return GitHubAPIResult(response=response)
            except httpx.RequestError as e:
                if attempt < retries:
                    delay = min(2**attempt, 2)
                    logger.warning(
                        "Request error, retrying",
                        url=url,
                        error=str(e),
                        attempt=attempt + 1,
                        max_retries=retries,
                        delay_seconds=delay,
                        **context,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error("Request error", url=url, error=str(e), **context)
                return GitHubAPIResult(
                    response=None,
                    should_queue=True,
                    error_type="network_error",
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < retries:
                    delay = min(2**attempt, 2)
                    logger.warning(
                        "Server error, retrying",
                        url=url,
                        status_code=e.response.status_code,
                        attempt=attempt + 1,
                        max_retries=retries,
                        delay_seconds=delay,
                        **context,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(
                    "HTTP error",
                    url=url,
                    status_code=e.response.status_code,
                    response_text=e.response.text,
                    **context,
                )
                should_queue = e.response.status_code >= 500
                return GitHubAPIResult(
                    response=None,
                    should_queue=should_queue,
                    error_type="server_error" if should_queue else "client_error",
                )
            except Exception as e:
                logger.error("Unexpected error", url=url, error=str(e), **context)
                return GitHubAPIResult(response=None, should_queue=False)

        return GitHubAPIResult(
            response=None, should_queue=True, error_type="max_retries"
        )


_github_client: GitHubAPIClient | None = None
_github_actions_client: GitHubAPIClient | None = None


def get_github_client() -> GitHubAPIClient:
    global _github_client
    if _github_client is None:
        _github_client = GitHubAPIClient(settings.github_status_token)
    return _github_client


def get_github_actions_client() -> GitHubAPIClient:
    global _github_actions_client
    if _github_actions_client is None:
        _github_actions_client = GitHubAPIClient(settings.github_token)
    return _github_actions_client


async def update_commit_status(
    sha: str,
    state: str,
    git_repo: str,
    target_url: str | None = None,
    description: str | None = None,
    context: str = "builds/x86_64",
    db: "AsyncSession | None" = None,
) -> bool:
    if not git_repo:
        logger.error(
            "Missing git_repo for GitHub status update. Skipping status update."
        )
        return False

    if not sha:
        logger.error("Missing commit SHA. Skipping status update.")
        return False

    if sha == "0000000000000000000000000000000000000000":
        logger.warning(
            "Detected null SHA (branch deletion). Skipping status update.",
            git_repo=git_repo,
            sha=sha,
        )
        return False

    if state not in ["error", "failure", "pending", "success"]:
        logger.error(f"Invalid state '{state}'. Skipping status update.")
        return False

    url = f"https://api.github.com/repos/{git_repo}/statuses/{sha}"
    payload: dict[str, str] = {
        "state": state,
        "context": context,
    }

    if target_url:
        payload["target_url"] = target_url

    if description:
        payload["description"] = description

    client = get_github_client()
    ctx = {"git_repo": git_repo, "commit": sha}
    result = await client.request_with_result("post", url, json=payload, context=ctx)

    if result.response:
        logger.info(
            "Successfully updated GitHub status",
            git_repo=git_repo,
            commit=sha,
            state=state,
        )
        return True

    if result.should_queue and db:
        from app.services.github_task import GitHubTaskService

        await GitHubTaskService().queue_task(
            db,
            task_type="commit_status",
            method="post",
            url=url,
            payload=payload,
            context=ctx,
            retry_after=result.retry_after,
        )

    return False


async def create_pr_comment(
    git_repo: str,
    pr_number: int,
    comment: str,
    db: "AsyncSession | None" = None,
) -> bool:
    if not git_repo:
        logger.error("Missing git_repo for GitHub PR comment. Skipping PR comment.")
        return False

    if not pr_number:
        logger.error("Missing PR number. Skipping PR comment.")
        return False

    url = f"https://api.github.com/repos/{git_repo}/issues/{pr_number}/comments"
    payload = {"body": comment}
    ctx = {"git_repo": git_repo, "pr_number": pr_number}

    client = get_github_client()
    result = await client.request_with_result("post", url, json=payload, context=ctx)

    if result.response:
        logger.info(
            "Successfully created PR comment",
            git_repo=git_repo,
            pr_number=pr_number,
        )
        return True

    if result.should_queue and db:
        from app.services.github_task import GitHubTaskService

        await GitHubTaskService().queue_task(
            db,
            task_type="pr_comment",
            method="post",
            url=url,
            payload=payload,
            context=ctx,
            retry_after=result.retry_after,
        )

    return False


async def create_github_issue(
    git_repo: str, title: str, body: str
) -> tuple[str, int] | None:
    """Create a GitHub issue.

    Returns (issue_url, issue_number) on success, None on failure.
    """
    if not git_repo:
        logger.error("Missing git_repo for GitHub issue. Skipping issue creation.")
        return None

    url = f"https://api.github.com/repos/{git_repo}/issues"
    client = get_github_client()
    response = await client.request(
        "post",
        url,
        json={"title": title, "body": body},
        context={"git_repo": git_repo},
    )
    if response:
        data = response.json()
        issue_url = data.get("html_url", "unknown URL")
        issue_number = data.get("number")
        logger.info(
            "Successfully created GitHub issue",
            git_repo=git_repo,
            issue_url=issue_url,
            issue_number=issue_number,
        )
        if issue_number is not None:
            return (issue_url, issue_number)
    return None


async def close_github_issue(git_repo: str, issue_number: int) -> bool:
    if not git_repo:
        logger.error("Missing git_repo for GitHub issue. Skipping issue closure.")
        return False

    if not issue_number:
        logger.error("Missing issue number. Skipping issue closure.")
        return False

    url = f"https://api.github.com/repos/{git_repo}/issues/{issue_number}"
    client = get_github_client()
    response = await client.request(
        "patch",
        url,
        json={"state": "closed"},
        context={"git_repo": git_repo, "issue_number": issue_number},
    )
    if response:
        logger.info(
            "Successfully closed GitHub issue",
            git_repo=git_repo,
            issue_number=issue_number,
        )
        return True
    return False


async def get_github_issue(git_repo: str, issue_number: int) -> dict | None:
    """Get a GitHub issue by number.

    Returns {"state": "open"|"closed", "state_reason": "completed"|"not_planned"|None}
    on success, None on failure.
    """
    if not git_repo:
        logger.error("Missing git_repo for GitHub issue. Skipping issue fetch.")
        return None

    if not issue_number:
        logger.error("Missing issue number. Skipping issue fetch.")
        return None

    url = f"https://api.github.com/repos/{git_repo}/issues/{issue_number}"
    client = get_github_client()
    response = await client.request(
        "get",
        url,
        context={"git_repo": git_repo, "issue_number": issue_number},
    )
    if response:
        data = response.json()
        result = {
            "state": data.get("state"),
            "state_reason": data.get("state_reason"),
        }
        logger.info(
            "Successfully fetched GitHub issue",
            git_repo=git_repo,
            issue_number=issue_number,
            state=result["state"],
            state_reason=result["state_reason"],
        )
        return result
    return None


async def reopen_github_issue(git_repo: str, issue_number: int) -> bool:
    if not git_repo:
        logger.error("Missing git_repo for GitHub issue. Skipping issue reopen.")
        return False

    if not issue_number:
        logger.error("Missing issue number. Skipping issue reopen.")
        return False

    url = f"https://api.github.com/repos/{git_repo}/issues/{issue_number}"
    client = get_github_client()
    response = await client.request(
        "patch",
        url,
        json={"state": "open"},
        context={"git_repo": git_repo, "issue_number": issue_number},
    )
    if response:
        logger.info(
            "Successfully reopened GitHub issue",
            git_repo=git_repo,
            issue_number=issue_number,
        )
        return True
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
    client = get_github_client()
    context = {"git_repo": git_repo, "issue_number": issue_number}

    if check_duplicates:
        get_response = await client.request("get", url, context=context)
        if get_response:
            for existing in get_response.json():
                if comment in existing.get("body", ""):
                    logger.info(
                        "Comment with same body already exists on GitHub issue. Skipping.",
                        git_repo=git_repo,
                        issue_number=issue_number,
                    )
                    return True

    response = await client.request(
        "post", url, json={"body": comment}, context=context
    )
    if response:
        logger.info(
            "Successfully added comment to GitHub issue",
            git_repo=git_repo,
            issue_number=issue_number,
        )
        return True
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
    client = get_github_client()
    response = await client.request("get", url, context={"run_id": run_id})
    if response:
        run_data = response.json()
        title = run_data.get("display_title", "") or run_data.get("name", "")
        logger.info(
            "Successfully fetched workflow run title",
            run_id=run_id,
            title=title,
        )
        return title
    return None


async def get_build_job_arches(
    run_id: int, owner: str = "flathub-infra", repo: str = "vorarbeiter"
) -> list[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    client = get_github_client()
    response = await client.request(
        "get", url, context={"owner": owner, "repo": repo, "run_id": run_id}
    )
    if response:
        jobs = response.json().get("jobs", [])
        return [
            job["name"].removeprefix("build-").strip()
            for job in jobs
            if job.get("name", "").startswith("build-")
        ]
    return []


async def get_check_run_annotations(
    owner: str,
    repo: str,
    run_id: int,
    job_filter: Callable[[dict], bool] | None = None,
) -> list[dict] | None:
    client = get_github_client()
    context = {"owner": owner, "repo": repo, "run_id": run_id}

    jobs_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    response = await client.request("get", jobs_url, context=context)
    if not response:
        return None

    jobs = response.json().get("jobs", [])
    annotations: list[dict[str, str | None]] = []

    for job in jobs:
        if job_filter and not job_filter(job):
            continue

        if check_run_url := job.get("check_run_url"):
            annotations_response = await client.request(
                "get",
                f"{check_run_url}/annotations",
                context={**context, "job_id": job.get("id")},
                raise_for_status=False,
            )
            if annotations_response and annotations_response.status_code == 200:
                annotations.extend(
                    {
                        "message": a.get("message"),
                        "annotation_level": a.get("annotation_level"),
                    }
                    for a in annotations_response.json()
                )

    logger.info(
        "Successfully fetched check-run annotations",
        owner=owner,
        repo=repo,
        run_id=run_id,
        annotation_count=len(annotations),
    )
    return annotations


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
