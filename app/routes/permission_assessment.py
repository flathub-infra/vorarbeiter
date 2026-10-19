from typing import Annotated

import jwt
import structlog
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.services.permission_assessment import refresh_permission_review

logger = structlog.get_logger(__name__)


class PermissionDecisionNotification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_repository: str = Field(min_length=1)
    pull_request_number: int = Field(gt=0)


def _authenticate(
    authorization: Annotated[
        HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))
    ],
) -> None:
    secret = settings.permission_assessment_secret
    if not secret:
        raise HTTPException(
            status_code=503, detail="permission_assessment_not_configured"
        )
    if authorization is None:
        raise HTTPException(
            status_code=401,
            detail="invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = jwt.decode(
            authorization.credentials,
            secret,
            algorithms=["HS256"],
            options={"require": ["sub", "iat", "exp"]},
        )
        if claims["sub"] != "permission-assessment":
            raise jwt.InvalidTokenError("Invalid assessment subject")
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=401,
            detail="invalid_token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    scope = claims.get("scope")
    scopes = scope.split() if isinstance(scope, str) else scope
    if not isinstance(scopes, list) or "notify" not in scopes:
        raise HTTPException(status_code=403, detail="invalid_scope")


permission_assessment_router = APIRouter(
    prefix="/api/webhooks",
    tags=["webhooks"],
    dependencies=[Depends(_authenticate)],
)


@permission_assessment_router.post("/permission-decisions", status_code=204)
async def permission_decision(
    notification: PermissionDecisionNotification,
) -> Response:
    try:
        await refresh_permission_review(
            notification.source_repository, notification.pull_request_number
        )
    except Exception as exc:
        logger.exception(
            "Failed to refresh permission review",
            source_repository=notification.source_repository,
            pull_request_number=notification.pull_request_number,
        )
        raise HTTPException(
            status_code=503, detail="permission_refresh_failed"
        ) from exc
    return Response(status_code=204)
