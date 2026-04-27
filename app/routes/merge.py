import uuid

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi import status as http_status

from app.services import merge_service
from app.services.merge import (
    MergeCallbackConflictError,
    MergeInvalidTokenError,
    MergeNotFoundError,
)

logger = structlog.get_logger(__name__)
merge_router = APIRouter(prefix="/api/merge", tags=["merge"])


@merge_router.post(
    "/{merge_id}/callback",
    status_code=http_status.HTTP_200_OK,
)
async def merge_callback(merge_id: uuid.UUID, request: Request):
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
        )
    callback_token = auth_header.removeprefix("Bearer ")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    status = body.get("status")
    if status not in ("success", "failure"):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Invalid status value",
        )

    try:
        await merge_service.handle_callback(merge_id, callback_token, status)
    except MergeNotFoundError as err:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )
    except MergeInvalidTokenError as err:
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail=str(err),
        )
    except MergeCallbackConflictError as err:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=str(err),
        )
    return {"status": "ok", "merge_id": str(merge_id)}
