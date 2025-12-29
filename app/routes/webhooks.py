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
from app.utils.flat_manager import FlatManagerClient
from app.utils.github import (
    add_issue_comment,
    close_github_issue,
    create_pr_comment,
    update_commit_status,
    is_issue_edited,
    get_workflow_run_title,
)

logger = structlog.get_logger(__name__)

webhooks_router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

STABLE_BUILD_FAILURE_PATTERN = re.compile(
    r"The stable build pipeline for `.+?` failed\.\s*\n"
    r"Commit SHA: ([0-9a-fA-F]+)\s*\n"
    r"Build log: (https://github\.com/flathub-infra/vorarbeiter/actions/runs/\d+)"
)
JOB_FAILURE_PATTERN = re.compile(
    r"The (\w+) job for `.+?` failed in the (\w+) repository\.\n\n.*?-? ?Commit SHA: ([0-9a-fA-F]+)",
    re.DOTALL,
)


async def parse_failure_issue(issue_body: str, git_repo: str) -> dict | None:
    stable_match = STABLE_BUILD_FAILURE_PATTERN.search(issue_body)
    if stable_match:
        sha, build_url = stable_match.groups()
        run_id = int(build_url.rstrip("/").split("/")[-1])

        ref = "refs/heads/master"

        title = await get_workflow_run_title(run_id)
        if title:
            ref_match = re.search(r"from (refs/heads/\S+)", title)
            if ref_match:
                extracted_ref = ref_match.group(1)
                if extracted_ref in (
                    "refs/heads/master",
                    "refs/heads/beta",
                ) or extracted_ref.startswith("refs/heads/branch/"):
                    ref = extracted_ref

        if ref == "refs/heads/beta":
            flat_mgr_repo = "beta"
        else:
            flat_mgr_repo = "stable"

        return {
            "sha": sha,
            "repo": git_repo,
            "ref": ref,
            "flat_manager_repo": flat_mgr_repo,
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
    if "/" not in git_repo:
        logger.warning(
            "Invalid repository format", repo=git_repo, issue_number=issue_number
        )
        return None

    was_edited = await is_issue_edited(git_repo=git_repo, issue_number=issue_number)

    if was_edited:
        logger.info(
            "Issue body was edited, aborting retry",
            repo=git_repo,
            issue_number=issue_number,
        )
        await add_issue_comment(
            git_repo=git_repo,
            issue_number=issue_number,
            comment="❌ Unable to retry as the issue was edited.",
        )
        return None
    elif was_edited is None:
        logger.error(
            "Failed to check issue edit status, aborting retry",
            repo=git_repo,
            issue_number=issue_number,
        )
        await add_issue_comment(
            git_repo=git_repo,
            issue_number=issue_number,
            comment="❌ Failed to verify issue status. Please see the logs.",
        )
        return None

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

    app_id = git_repo.split("/", 1)[1]

    build_params = await parse_failure_issue(issue_body, git_repo)
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
            comment=f"🔄 Retrying build: [view build]({build_url})",
        )

        await close_github_issue(git_repo=git_repo, issue_number=issue_number)

        logger.info(
            "Successfully triggered retry build",
            pipeline_id=str(pipeline.id),
            repo=git_repo,
            issue_number=issue_number,
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
    - Comment contains "bot, ping admins" not inside quotes or inline code blocks
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
            if line.lstrip().startswith(("`", "<code>")) and line.rstrip().endswith(
                ("`", "</code>")
            ):
                continue
            if any(
                s in line
                for s in (
                    "`bot, build`",
                    "<code>bot, build</code>",
                    "`bot, retry`",
                    "<code>bot, retry</code>",
                    "`bot, ping admins`",
                    "<code>bot, ping admins</code>",
                )
            ):
                continue
            comment_lines.append(line)
        filtered_comment = "\n".join(comment_lines)

        if "bot, build" in filtered_comment:
            return True

        if "bot, retry" in filtered_comment.lower():
            return True

        if "bot, ping admins" in filtered_comment.lower():
            return True

    return False


async def fetch_flathub_json(
    repo: str,
    ref: str,
    github_token: str | None = None,
) -> dict[str, Any] | None:
    url = f"https://api.github.com/repos/{repo}/contents/flathub.json?ref={ref}"
    headers = {"Accept": "application/vnd.github.raw+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                logger.warning("flathub.json is not a JSON object", repo=repo, ref=ref)
                return {}
            return data
    except (httpx.HTTPError, ValueError) as err:
        logger.error("Error fetching flathub.json from GitHub", error=str(err))
        return None


def get_eol_only_changes(
    base_json: dict[str, Any],
    head_json: dict[str, Any],
) -> dict[str, str] | None:
    eol_keys = {"end-of-life", "end-of-life-rebase"}

    def coerce(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    all_keys = set(base_json.keys()) | set(head_json.keys())
    changed_keys = {k for k in all_keys if base_json.get(k) != head_json.get(k)}

    if not changed_keys or not changed_keys.issubset(eol_keys):
        return None

    eol_data: dict[str, str] = {}
    if "end-of-life" in changed_keys:
        eol_data["end_of_life"] = coerce(head_json.get("end-of-life", ""))
    if "end-of-life-rebase" in changed_keys:
        eol_data["end_of_life_rebase"] = coerce(head_json.get("end-of-life-rebase", ""))

    return eol_data


async def is_eol_only_pr(
    payload: dict[str, Any], github_token: str | None = None
) -> tuple[bool, dict[str, str] | None]:
    repo = payload.get("repository", {}).get("full_name")
    pr = payload.get("pull_request", {})
    number = pr.get("number")
    base_ref = pr.get("base", {}).get("sha")
    head_ref = pr.get("head", {}).get("sha")

    if not (repo and number and base_ref and head_ref):
        return False, None

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
        return False, None

    if not files or len(files) != 1:
        return False, None

    file_info = files[0]
    if file_info.get("filename") != "flathub.json":
        return False, None

    base_json = await fetch_flathub_json(repo, base_ref, github_token)
    head_json = await fetch_flathub_json(repo, head_ref, github_token)
    if base_json is None or head_json is None:
        return False, None

    eol_data = get_eol_only_changes(base_json, head_json)
    return (eol_data is not None, eol_data)


async def is_eol_only_push(
    payload: dict[str, Any], github_token: str | None = None
) -> tuple[bool, dict[str, str] | None]:
    repo = payload.get("repository", {}).get("full_name")
    before = payload.get("before")
    after = payload.get("after")

    if not (repo and before and after):
        return False, None

    zero_sha = "0" * 40
    if before == zero_sha or after == zero_sha:
        return False, None

    url = f"https://api.github.com/repos/{repo}/compare/{before}...{after}"
    headers = {}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            comparison = r.json()
    except (httpx.HTTPError, ValueError) as err:
        logger.error("Error fetching compare details from GitHub", error=str(err))
        return False, None

    files = comparison.get("files", [])
    if not files or len(files) != 1:
        return False, None

    file_info = files[0]
    if file_info.get("filename") != "flathub.json":
        return False, None

    base_json = await fetch_flathub_json(repo, before, github_token)
    head_json = await fetch_flathub_json(repo, after, github_token)
    if base_json is None or head_json is None:
        return False, None

    eol_data = get_eol_only_changes(base_json, head_json)
    return (eol_data is not None, eol_data)


async def handle_eol_only_pr(
    payload: dict[str, Any], eol_data: dict[str, str] | None
) -> None:
    repo = payload.get("repository", {}).get("full_name")
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number")
    sha = pr.get("head", {}).get("sha")

    if not (repo and sha):
        return

    logger.info(
        "Handling EOL-only PR",
        repo=repo,
        pr_number=pr_number,
        eol_data=eol_data,
    )

    await update_commit_status(
        sha=sha,
        state="success",
        git_repo=repo,
        description="EOL-only change - build skipped",
    )

    if not pr_number:
        return

    def format_value(value: str | None) -> str:
        if value is None:
            return "not set"
        if value == "":
            return "<empty>"
        return value

    end_of_life = eol_data.get("end_of_life") if eol_data else None
    end_of_life_rebase = eol_data.get("end_of_life_rebase") if eol_data else None

    comment = (
        "EOL-only change detected in `flathub.json`; build skipped.\n\n"
        "Detected values:\n"
        f"- end-of-life: `{format_value(end_of_life)}`\n"
        f"- end-of-life-rebase: `{format_value(end_of_life_rebase)}`\n\n"
        "Flat-manager will republish after this PR is merged."
    )
    await create_pr_comment(git_repo=repo, pr_number=pr_number, comment=comment)


async def handle_eol_only_push(
    event: WebhookEvent,
    ref: str | None,
    sha: str | None,
    eol_data: dict[str, str] | None,
) -> None:
    if not ref or not sha:
        return

    if ref == "refs/heads/master":
        flat_manager_repo = "stable"
    elif ref == "refs/heads/beta":
        flat_manager_repo = "beta"
    elif ref.startswith("refs/heads/branch/"):
        flat_manager_repo = "stable"
    else:
        logger.info(
            "Skipping EOL-only republish for non-production ref",
            repo=event.repository,
            ref=ref,
        )
        return

    await update_commit_status(
        sha=sha,
        state="pending",
        git_repo=event.repository,
        description="EOL-only change - republish queued",
    )

    app_id = event.repository.split("/", 1)[1]
    end_of_life = eol_data.get("end_of_life") if eol_data else None
    end_of_life_rebase = eol_data.get("end_of_life_rebase") if eol_data else None

    flat_manager = FlatManagerClient(
        url=settings.flat_manager_url, token=settings.flat_manager_token
    )
    try:
        await flat_manager.republish(
            repo=flat_manager_repo,
            app_id=app_id,
            end_of_life=end_of_life,
            end_of_life_rebase=end_of_life_rebase,
        )
    except Exception as err:
        logger.error(
            "Failed to republish EOL-only change",
            repo=event.repository,
            ref=ref,
            sha=sha,
            error=str(err),
        )
        await update_commit_status(
            sha=sha,
            state="failure",
            git_repo=event.repository,
            description="EOL-only republish failed",
        )
        return

    await update_commit_status(
        sha=sha,
        state="success",
        git_repo=event.repository,
        description="EOL-only republish complete",
    )


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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
            return {"message": "Webhook received but ignored due to PR changes filter."}

        is_eol_only, eol_data = await is_eol_only_pr(
            payload, settings.github_status_token
        )
        if is_eol_only:
            await handle_eol_only_pr(payload, eol_data)
            return {"message": "EOL-only PR - build skipped"}

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
                event_id=str(event.id) if event else None,
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
    params: dict[str, Any] = {"repo": event.repository}
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

        is_eol_only, eol_data = await is_eol_only_push(
            payload, settings.github_status_token
        )
        if is_eol_only:
            await handle_eol_only_push(event, ref, sha, eol_data)
            return None

    elif "comment" in payload:
        comment_body = payload.get("comment", {}).get("body", "").lower()
        issue = payload.get("issue", {})
        issue_number = issue.get("number")
        issue_body = issue.get("body", "")
        comment_author = payload.get("comment", {}).get("user", {}).get("login", "")
        issue_author = issue.get("user", {}).get("login", "")
        pr_url = issue.get("pull_request", {}).get("url", "")
        repo = event.repository

        if "bot, ping admins" in comment_body and repo not in ("flathub/flathub",):
            if issue_number is None:
                logger.error("Missing issue number for admin ping")
            else:
                logger.info("Handling admin ping")
                await add_issue_comment(
                    git_repo=repo,
                    issue_number=issue_number,
                    comment="Contacted Flathub admins: cc @flathub/build-moderation",
                    check_duplicates=True,
                )
            return None

        elif "bot, build" in comment_body:
            if not pr_url or issue_number is None:
                return None

            pr_ref = f"refs/pull/{issue_number}/head"

            github_api_url = f"https://api.github.com/repos/{repo}/pulls/{issue_number}"
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
                            pr_number=issue_number,
                            repo=repo,
                            pr_state=pr_state,
                        )
                        await create_pr_comment(
                            git_repo=repo,
                            pr_number=issue_number,
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
                    "pr_number": str(issue_number),
                    "ref": pr_ref,
                    "use_spot": False,
                }
            )

        elif "bot, retry" in comment_body:
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
                logger.info(
                    "Retry comment on PR, ignoring (only for build failure issues)"
                )
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
