import hashlib
import hmac
import re
import uuid

import httpx
import structlog
from fastapi import APIRouter, Header, HTTPException, Request, status
from typing import Any
from app.config import settings
from app.database import get_db
from app.models.webhook_event import WebhookEvent, WebhookSource
from app.pipelines.build import BuildPipeline, app_build_types
from app.utils.github import (
    add_issue_comment,
    close_github_issue,
    create_pr_comment,
    update_commit_status,
)

logger = structlog.get_logger(__name__)

webhooks_router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

STABLE_BUILD_FAILURE_PATTERN = re.compile(
    r"The stable build pipeline for `.+?` failed\.\n\nCommit SHA: ([0-9a-fA-F]+)"
)
JOB_FAILURE_PATTERN = re.compile(
    r"The (\w+) job for `.+?` failed in the (\w+) repository\.\n\n.*?Commit SHA: ([0-9a-fA-F]+)",
    re.DOTALL,
)


def parse_failure_issue(issue_body: str, git_repo: str) -> dict | None:
    stable_match = STABLE_BUILD_FAILURE_PATTERN.search(issue_body)
    if stable_match:
        sha = stable_match.group(1)
        return {
            "sha": sha,
            "repo": git_repo,
            "ref": "refs/heads/master",
            "flat_manager_repo": "stable",
            "issue_type": "build_failure",
        }

    job_match = JOB_FAILURE_PATTERN.search(issue_body)
    if job_match:
        job_type, repo_type, sha = job_match.groups()
        ref = (
            "refs/heads/master" if repo_type.lower() == "stable" else "refs/heads/beta"
        )
        return {
            "sha": sha,
            "repo": git_repo,
            "ref": ref,
            "flat_manager_repo": repo_type.lower(),
            "issue_type": "job_failure",
            "job_type": job_type,
        }

    return None


async def validate_retry_permissions(git_repo: str, user_login: str) -> bool:
    if not settings.github_status_token:
        logger.warning("GITHUB_STATUS_TOKEN not set. Skipping permission check.")
        return False

    url = f"https://api.github.com/repos/{git_repo}/collaborators/{user_login}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {settings.github_status_token}",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            if response.status_code == 204:
                return True
            elif response.status_code == 404:
                logger.info(
                    "User not a collaborator, checking organization membership",
                    user=user_login,
                    repo=git_repo,
                )

                org = git_repo.split("/")[0]
                org_url = f"https://api.github.com/orgs/{org}/members/{user_login}"
                org_response = await client.get(org_url, headers=headers, timeout=10.0)
                return org_response.status_code == 204
            else:
                logger.warning(
                    "Unexpected response checking user permissions",
                    status_code=response.status_code,
                    user=user_login,
                    repo=git_repo,
                )
                return False
    except Exception as e:
        logger.error(
            "Error checking retry permissions",
            error=str(e),
            user=user_login,
            repo=git_repo,
        )
        return False


async def handle_issue_retry(
    git_repo: str,
    issue_number: int,
    issue_body: str,
    comment_author: str,
    webhook_event_id: uuid.UUID,
) -> uuid.UUID | None:
    if not await validate_retry_permissions(git_repo, comment_author):
        logger.warning(
            "User does not have permission to trigger retries",
            user=comment_author,
            repo=git_repo,
            issue_number=issue_number,
        )
        await add_issue_comment(
            git_repo=git_repo,
            issue_number=issue_number,
            comment=f"❌ @{comment_author} does not have permission to trigger retries.",
        )
        return None

    if "/" not in git_repo:
        logger.warning(
            "Invalid repository format", repo=git_repo, issue_number=issue_number
        )
        return None

    app_id = git_repo.split("/", 1)[1]

    build_params = parse_failure_issue(issue_body, git_repo)
    if not build_params:
        logger.warning(
            "Could not parse build parameters from issue",
            repo=git_repo,
            issue_number=issue_number,
        )
        await add_issue_comment(
            git_repo=git_repo,
            issue_number=issue_number,
            comment="❌ Could not parse build parameters from this issue. This may not be a valid build failure issue.",
        )
        return None

    build_params["app_id"] = app_id

    retry_count = 1
    existing_retry_pattern = re.search(
        r"This is retry (\d+) of the original build", issue_body
    )
    if existing_retry_pattern:
        retry_count = int(existing_retry_pattern.group(1)) + 1
        if retry_count > 3:
            await add_issue_comment(
                git_repo=git_repo,
                issue_number=issue_number,
                comment="❌ Maximum retry limit (3) reached for this build. Please investigate the underlying issue.",
            )
            return None

    build_params["retry_count"] = retry_count
    build_params["retry_from_issue"] = issue_number

    try:
        pipeline_service = BuildPipeline()
        pipeline = await pipeline_service.create_pipeline(
            app_id=build_params["app_id"],
            params=build_params,
            webhook_event_id=webhook_event_id,
        )

        target_url = f"{settings.base_url}/api/pipelines/{pipeline.id}"
        await update_commit_status(
            sha=build_params["sha"],
            state="pending",
            git_repo=git_repo,
            description="Retry build enqueued",
            target_url=target_url,
        )

        pipeline = await pipeline_service.start_pipeline(pipeline_id=pipeline.id)

        build_url = f"{settings.base_url}/api/pipelines/{pipeline.id}/log_url"
        await add_issue_comment(
            git_repo=git_repo,
            issue_number=issue_number,
            comment=f"🔄 Retrying build (attempt {retry_count}): [view build]({build_url})",
        )

        await close_github_issue(git_repo=git_repo, issue_number=issue_number)

        logger.info(
            "Successfully triggered retry build",
            pipeline_id=str(pipeline.id),
            repo=git_repo,
            issue_number=issue_number,
            retry_count=retry_count,
            triggered_by=comment_author,
        )

        return pipeline.id

    except Exception as e:
        logger.error(
            "Failed to trigger retry build",
            error=str(e),
            repo=git_repo,
            issue_number=issue_number,
        )
        await add_issue_comment(
            git_repo=git_repo,
            issue_number=issue_number,
            comment=f"❌ Failed to trigger retry build: {str(e)}",
        )
        return None


def should_store_event(payload: dict) -> bool:
    """
    Determine if a webhook event should be stored based on event type.

    Store events only when:
    - A new PR is opened
    - A PR is updated
    - A new commit happens to master, beta or branch/*
    - PR comment contains "bot, build" not inside quotes or inline code blocks
    - Issue comment contains "bot, retry" not inside quotes or inline code blocks
    """
    ref = payload.get("ref", "")
    comment = payload.get("comment", {}).get("body", "")

    if "pull_request" in payload:
        pr_action = payload.get("action", "")
        # If the PR is not meant to merged in an "official" branch
        # no point in triggerring a build from that
        # If ref is None for whatever reason it falls back to returning True
        target_ref = payload.get("pull_request", {}).get("base", {}).get("ref")
        if pr_action in ["opened", "synchronize", "reopened"]:
            return (
                not target_ref
                or target_ref in ("master", "beta")
                or target_ref.startswith("branch/")
            )

    if "commits" in payload and ref:
        if ref in (
            "refs/heads/master",
            "refs/heads/beta",
        ) or ref.startswith("refs/heads/branch/"):
            return True

    if "comment" in payload:
        repo_full_name = payload.get("repository", {}).get("full_name")
        comment_author = payload.get("comment", {}).get("user", {}).get("login")

        if comment_author in ("github-actions[bot]",) and repo_full_name not in (
            "flathub/flathub",
        ):
            return False

        comment_lines = []
        for line in comment.splitlines():
            if line.lstrip().startswith(">"):
                continue
            if line.lstrip().startswith("`") and line.lstrip().endswith("`"):
                continue
            if "`bot, build`" in line:
                continue
            comment_lines.append(line)
        filtered_comment = "\n".join(comment_lines)

        if "bot, build" in filtered_comment:
            return True

        if "bot, retry" in filtered_comment.lower():
            return True

    return False


async def is_submodule_only_pr(
    payload: dict[str, Any], github_token: str | None = None
) -> bool:
    repo, number = (
        payload.get("repository", {}).get("full_name"),
        payload.get("pull_request", {}).get("number"),
    )

    if not (repo and number):
        return False

    # Public API, token is only required if requests exceed some per
    # minute limit
    url = f"https://api.github.com/repos/{repo}/pulls/{number}/files"
    headers = {}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            files = r.json()
    except (httpx.HTTPError, ValueError) as err:
        logger.error("Error fetching PR file details from GitHub", error=str(err))
        return False

    if not files:
        return False

    return all(
        "patch" in f and f["patch"] and "Subproject commit" in f["patch"] for f in files
    )


@webhooks_router.post(
    "/github",
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_github_webhook(
    request: Request,
    x_github_delivery: str | None = Header(None, description="GitHub delivery GUID"),
    x_hub_signature_256: str | None = Header(
        None, description="GitHub webhook signature"
    ),
):
    if not x_github_delivery:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-GitHub-Delivery header.",
        )

    try:
        delivery_id = uuid.UUID(x_github_delivery)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-GitHub-Delivery header format (must be a UUID).",
        )

    if settings.github_webhook_secret:
        if not x_hub_signature_256:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-Hub-Signature-256 header.",
            )

        body = await request.body()
        secret = settings.github_webhook_secret.encode()
        signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
        expected_signature = f"sha256={signature}"

        if not hmac.compare_digest(expected_signature, x_hub_signature_256):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid signature.",
            )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        )

    try:
        repo_name = payload["repository"]["full_name"]
        actor_login = payload["sender"]["login"]
    except KeyError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing expected key in GitHub payload: {e}",
        )

    ignored_repos = [
        "flathub/flathub",
        "flathub/org.freedesktop.Platform.GL.nvidia",
        "flathub/shared-modules",
    ]
    is_pr_event = "pull_request" in payload and payload.get("action") in [
        "opened",
        "synchronize",
        "reopened",
    ]
    is_push_event = "commits" in payload and payload.get("ref", "")

    if repo_name in ignored_repos and (is_pr_event or is_push_event):
        return {"message": "Webhook received but ignored due to repository filter."}

    if is_pr_event:
        if repo_name.split("/")[1] in app_build_types:
            return {
                "message": "Pull request webhook received but ignored due to large app."
            }

        if actor_login in ("dependabot[bot]", "renovate[bot]"):
            return {"message": "Webhook received but ignored due to actor filter."}

        if actor_login in ("github-actions[bot]",) and await is_submodule_only_pr(
            payload
        ):
            return {"message": "Webhook received but ignored PR changes filter."}

    event = WebhookEvent(
        id=delivery_id,
        source=WebhookSource.GITHUB,
        payload=payload,
        repository=repo_name,
        actor=actor_login,
    )

    pipeline_id = None
    if should_store_event(payload):
        try:
            async with get_db() as db:
                db.add(event)
                await db.commit()

            pipeline_id = await create_pipeline(event)

        except Exception as e:
            logger.error(
                "Database error",
                error=str(e),
                event={"event_id": str(event.id) if event else None},
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database error occurred while processing webhook: {e}",
            )

    response = {"message": "Webhook received", "event_id": str(event.id)}
    if pipeline_id:
        response["pipeline_id"] = str(pipeline_id)

    return response


async def create_pipeline(event: WebhookEvent) -> uuid.UUID | None:
    payload = event.payload
    app_id = f"{event.repository.split('/')[-1]}"
    params = {"repo": event.repository}
    sha = None

    if "pull_request" in payload and payload.get("action") in [
        "opened",
        "synchronize",
        "reopened",
    ]:
        pr = payload.get("pull_request", {})
        pr_state = pr.get("state")

        if pr_state == "closed":
            logger.info(
                "PR is closed, skipping pipeline creation",
                pr_number=pr.get("number"),
                repo=event.repository,
                action=payload.get("action"),
            )
            return None

        pr_number = pr.get("number")
        sha = pr.get("head", {}).get("sha")
        params.update(
            {
                "ref": f"refs/pull/{pr_number}/head",
                "pr_number": str(pr_number) if pr_number is not None else "",
                "action": str(payload.get("action", "")),
            }
        )

    elif "commits" in payload and payload.get("ref", ""):
        ref = payload.get("ref", "")
        sha = payload.get("after")
        params.update(
            {
                "ref": ref,
                "push": "true",
            }
        )

    elif "comment" in payload and "bot, build" in payload.get("comment", {}).get(
        "body", ""
    ):
        issue = payload.get("issue", {})
        pr_url = issue.get("pull_request", {}).get("url", "")
        pr_number = issue.get("number")
        repo = event.repository

        if not pr_url or pr_number is None:
            return None

        pr_ref = f"refs/pull/{pr_number}/head"

        github_api_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {settings.github_status_token}",
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(github_api_url, headers=headers)
                response.raise_for_status()
                pr_data = response.json()
                sha = pr_data.get("head", {}).get("sha")

                pr_state = pr_data.get("state")
                if pr_state in ["closed", "merged"]:
                    logger.info(
                        "PR is closed/merged, ignoring 'bot, build' command",
                        pr_number=pr_number,
                        repo=repo,
                        pr_state=pr_state,
                    )
                    await create_pr_comment(
                        git_repo=repo,
                        pr_number=pr_number,
                        comment="❌ Cannot build closed or merged PR. Please reopen the PR if you want to trigger a build.",
                    )
                    return None
        except httpx.RequestError as e:
            logger.error("Error fetching PR details from GitHub", error=str(e))
        except httpx.HTTPStatusError as e:
            logger.error(
                "GitHub API error",
                status_code=e.response.status_code,
                response_text=e.response.text,
            )

        params.update(
            {
                "pr_number": str(pr_number),
                "ref": pr_ref,
            }
        )

    elif (
        "comment" in payload
        and "bot, retry" in payload.get("comment", {}).get("body", "").lower()
    ):
        issue = payload.get("issue", {})
        issue_number = issue.get("number")
        issue_body = issue.get("body", "")
        comment_author = payload.get("comment", {}).get("user", {}).get("login", "")
        issue_author = issue.get("user", {}).get("login", "")

        if not issue_number or not issue_body:
            logger.error("Missing issue number or body for retry request")
            return None

        if issue_author != "flathubbot":
            logger.info(
                "Retry comment on issue not created by flathubbot, ignoring",
                issue_author=issue_author,
                issue_number=issue_number,
            )
            return None

        if issue.get("pull_request"):
            logger.info("Retry comment on PR, ignoring (only for build failure issues)")
            return None

        retry_pipeline_id = await handle_issue_retry(
            git_repo=event.repository,
            issue_number=issue_number,
            issue_body=issue_body,
            comment_author=comment_author,
            webhook_event_id=event.id,
        )

        return retry_pipeline_id

    if sha:
        params["sha"] = sha

    pipeline_service = BuildPipeline()
    pipeline = await pipeline_service.create_pipeline(
        app_id=app_id,
        params=params,
        webhook_event_id=event.id,
    )

    commit_sha = pipeline.params.get("sha")
    git_repo = pipeline.params.get("repo")

    if commit_sha and git_repo:
        target_url = f"{settings.base_url}/api/pipelines/{pipeline.id}"
        await update_commit_status(
            sha=commit_sha,
            state="pending",
            git_repo=git_repo,
            description="Build enqueued",
            target_url=target_url,
        )
    elif commit_sha and not git_repo:
        logger.error(
            "Missing git_repo in params. Cannot update commit status.",
            pipeline_id=str(pipeline.id),
        )

    pipeline = await pipeline_service.start_pipeline(pipeline_id=pipeline.id)

    pr_number_str = pipeline.params.get("pr_number")
    if pr_number_str and git_repo:
        try:
            pr_number = int(pr_number_str)
            await create_pr_comment(
                git_repo=git_repo,
                pr_number=pr_number,
                comment="🚧 Test build [enqueued](https://github.com/flathub-infra/vorarbeiter/actions/workflows/build.yml).",
            )
        except ValueError:
            logger.error(
                "Invalid PR number. Skipping PR comment.",
                pr_number=pr_number_str,
                pipeline_id=str(pipeline.id),
            )
        except Exception as e:
            logger.error(
                "Error creating initial PR comment",
                pipeline_id=str(pipeline.id),
                error=str(e),
            )
    elif pr_number_str and not git_repo:
        logger.error(
            "Missing git_repo in params. Cannot create PR comment.",
            pipeline_id=str(pipeline.id),
        )

    return pipeline.id
