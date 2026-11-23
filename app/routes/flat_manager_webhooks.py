import hashlib
import hmac
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import ValidationError

from app.services.flat_manager_events import (
    JOB_FINISHED,
    FlatManagerEvent,
    JobFinished,
    record_event,
    webhook_key,
)

logger = structlog.get_logger(__name__)

MAX_BODY_BYTES = 64 * 1024
MAX_CLOCK_SKEW_SECONDS = 300

flat_manager_webhooks_router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


async def _read_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=400, detail="body_too_large")
    return bytes(body)


@flat_manager_webhooks_router.post("/flat-manager")
async def receive_flat_manager_webhook(
    request: Request,
    x_flatmanager_signature: str | None = Header(None),
) -> dict[str, str]:
    key = webhook_key()
    if key is None:
        raise HTTPException(status_code=503, detail="webhook_not_configured")

    body = await _read_body(request)
    expected = "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()
    if not x_flatmanager_signature or not hmac.compare_digest(
        expected, x_flatmanager_signature
    ):
        logger.warning("Invalid flat-manager webhook signature")
        raise HTTPException(status_code=401, detail="invalid_signature")

    try:
        event = FlatManagerEvent.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="malformed_payload") from exc

    skew = abs((datetime.now(tz=UTC) - event.created_at).total_seconds())
    if skew > MAX_CLOCK_SKEW_SECONDS:
        raise HTTPException(status_code=400, detail="stale_timestamp")

    if event.type != JOB_FINISHED:
        return {"status": "ignored"}

    try:
        JobFinished.model_validate(event.data)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="malformed_payload") from exc

    await record_event(event)
    return {"status": "accepted"}
