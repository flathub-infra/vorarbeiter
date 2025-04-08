import hmac
import hashlib
import uuid

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import settings
from app.database import get_db
from app.models.webhook_event import WebhookEvent, WebhookSource

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

    event = WebhookEvent(
        id=delivery_id,
        source=WebhookSource.GITHUB,
        payload=payload,
        repository=repo_name,
        actor=actor_login,
    )

    if should_store_event(payload):
        try:
            async with get_db() as db:
                db.add(event)
                await db.refresh(event)
        except Exception as e:
            print(f"Database error: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database error occurred while saving webhook event.",
            )

    return {"message": "Webhook received", "event_id": event.id}
