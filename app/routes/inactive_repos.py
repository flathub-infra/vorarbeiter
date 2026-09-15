from datetime import UTC

import structlog
from fastapi import APIRouter, HTTPException
from fastapi import status as http_status
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db
from app.models import InactiveRepoSnapshot

inactive_repos_router = APIRouter(prefix="/api", tags=["inactive-repos"])
logger = structlog.get_logger(__name__)


@inactive_repos_router.get("/inactive-repos.txt")
async def inactive_repositories() -> Response:
    try:
        async with get_db(use_replica=True) as db:
            snapshot = await db.get(InactiveRepoSnapshot, "flathub")
    except SQLAlchemyError as error:
        logger.exception("Inactive repository snapshot read failed")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Inactive repository snapshot unavailable",
        ) from error
    if snapshot is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inactive repository snapshot is not initialized",
        )
    effective = set(snapshot.automatic_candidates)
    body = "".join(f"{name}\n" for name in sorted(effective))
    completed_at = snapshot.scan_completed_at
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    return Response(
        content=body,
        media_type="text/plain",
        headers={
            "Cache-Control": "no-store",
            "X-Inactive-Repos-Snapshot-Completed-At": completed_at.astimezone(
                UTC
            ).isoformat(),
        },
    )
