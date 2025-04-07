import uuid

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.database import AsyncSessionLocal
from app.models.webhook_event import WebhookEvent, WebhookSource

webhooks_router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@webhooks_router.post(
    "/github",
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_github_webhook(
    request: Request,
    x_github_delivery: str | None = Header(None, description="GitHub delivery GUID"),
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

    try:
        async with AsyncSessionLocal() as db:
            db.add(event)
            await db.commit()
            await db.refresh(event)
    except Exception as e:
        print(f"Database error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error occurred while saving webhook event.",
        )

    return {"message": "Webhook received", "event_id": event.id}
