import uuid
import zipfile

import httpx2 as httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.database import get_db
from app.models import SmokeResult
from app.services.smoke_artifacts import API, download_artifact
from app.smoke import relative_path
from app.utils.github import get_github_actions_client

smoke_router = APIRouter(prefix="/smoke", tags=["smoke"])


@smoke_router.get("/{pipeline_id}/{artifact_id}/{image:path}")
async def screenshot(pipeline_id: uuid.UUID, artifact_id: int, image: str) -> Response:
    """Serve only a recorded preview from a matching, unexpired CI artifact."""
    try:
        relative_path(image)
    except ValueError:
        raise HTTPException(404, "Screenshot not found") from None
    async with get_db(use_replica=True) as db:
        result = await db.get(SmokeResult, pipeline_id)
        if (
            not result
            or result.artifact_id != artifact_id
            or image
            not in {preview["path"] for preview in result.summary.get("previews", [])}
        ):
            raise HTTPException(404, "Screenshot not found")
        run_id = result.run_id
    response = await get_github_actions_client().request(
        "get", f"{API}/actions/artifacts/{artifact_id}", raise_for_status=False
    )
    if response is None:
        raise HTTPException(502, "Screenshot storage unavailable")
    if response.status_code == 404:
        raise HTTPException(410, "Screenshot artifact is no longer available")
    if response.status_code != 200:
        raise HTTPException(502, "Screenshot storage unavailable")
    artifact = response.json()
    if artifact.get("workflow_run", {}).get("id") != run_id:
        raise HTTPException(404, "Screenshot not found")
    try:
        report = await download_artifact(artifact)
        data = report.images[image]
    except FileNotFoundError:
        raise HTTPException(410, "Screenshot artifact expired") from None
    except (
        ValueError,
        KeyError,
        TypeError,
        zipfile.BadZipFile,
        httpx.HTTPError,
        RuntimeError,
    ):
        raise HTTPException(502, "Screenshot artifact unavailable") from None
    return Response(
        data,
        media_type="image/png",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=600",
        },
    )
