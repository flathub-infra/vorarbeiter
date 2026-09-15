import re
from datetime import UTC
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException
from fastapi import status as http_status
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db
from app.models import InactiveRepoSnapshot

inactive_repos_router = APIRouter(prefix="/api", tags=["inactive-repos"])
logger = structlog.get_logger(__name__)
OVERRIDE_DIRECTORY = Path(__file__).resolve().parents[2] / "config" / "inactive-repos"
REPOSITORY_BASENAME = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)


def load_override_file(path: Path) -> set[str]:
    entries: set[str] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        entry = raw_line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry in {".", ".."} or REPOSITORY_BASENAME.fullmatch(entry) is None:
            raise ValueError(f"Malformed repository basename in {path}:{line_number}")
        entries.add(entry)
    return entries


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
    try:
        exclude = load_override_file(OVERRIDE_DIRECTORY / "exclude.txt")
        manual_inactive = load_override_file(OVERRIDE_DIRECTORY / "manual_inactive.txt")
    except (OSError, ValueError) as error:
        logger.exception("Inactive repository override read failed")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Inactive repository overrides unavailable",
        ) from error
    effective = (set(snapshot.automatic_candidates) - exclude) | manual_inactive
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
