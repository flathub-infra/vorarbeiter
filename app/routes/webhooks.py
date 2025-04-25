import hashlib
import hmac
import uuid
import logging

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import settings
from app.database import get_db
from app.models.webhook_event import WebhookEvent, WebhookSource
from app.pipelines.build import BuildPipeline
from app.utils.github import create_pr_comment, update_commit_status

webhooks_router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def should_store_event(payload: dict) -> bool:
    """
    Determine if a webhook event should be stored based on event type.

    Store events only when:
    - A new PR is opened
    - A PR is updated
    - A new commit happens to master, beta or branch/*
    - PR comment contains "bot, build"
    """
    ref = payload.get("ref", "")
    comment = payload.get("comment", {}).get("body", "")

    if "pull_request" in payload:
        pr_action = payload.get("action", "")
        if pr_action in ["opened", "synchronize", "reopened"]:
            return True

    if "commits" in payload and ref:
        if (
            ref.startswith("refs/heads/master")
            or ref.startswith("refs/heads/beta")
            or ref.startswith("refs/heads/branch/")
        ):
            return True

    if "comment" in payload and "bot, build" in comment:
        return True

    return False


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
    ]
    is_pr_event = "pull_request" in payload and payload.get("action") in [
        "opened",
        "synchronize",
        "reopened",
    ]
    is_push_event = "commits" in payload and payload.get("ref", "")

    if repo_name in ignored_repos and (is_pr_event or is_push_event):
        return {"message": "Webhook received but ignored due to repository filter."}

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
            print(f"Database error: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database error occurred while saving webhook event: {e}",
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
        except httpx.RequestError as e:
            print(f"Error fetching PR details from GitHub: {e}")
        except httpx.HTTPStatusError as e:
            print(f"GitHub API error: {e.response.status_code} - {e.response.text}")

        params.update(
            {
                "pr_number": str(pr_number),
                "ref": pr_ref,
            }
        )

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
        logging.error(
            f"Pipeline {pipeline.id}: Missing git_repo in params. Cannot update commit status."
        )

    pipeline = await pipeline_service.start_pipeline(pipeline_id=pipeline.id)

    pr_number_str = pipeline.params.get("pr_number")
    if pr_number_str and git_repo:
        try:
            pr_number = int(pr_number_str)
            await create_pr_comment(
                git_repo=git_repo,
                pr_number=pr_number,
                comment="🚧 Test build enqueued.",
            )
        except ValueError:
            print(
                f"Invalid pr_number '{pr_number_str}' for pipeline {pipeline.id}. Skipping PR comment."
            )
        except Exception as e:
            print(f"Error creating initial PR comment for pipeline {pipeline.id}: {e}")
    elif pr_number_str and not git_repo:
        logging.error(
            f"Pipeline {pipeline.id}: Missing git_repo in params. Cannot create PR comment."
        )

    return pipeline.id
