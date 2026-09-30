from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2 as httpx
import jwt
import structlog
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.services.job_monitor import JobMonitor
from app.utils.github import get_build_job_arches, normalize_git_oid

logger = structlog.get_logger(__name__)

PROVIDER_DATA_KEY = "permission_assessment"
MAX_ATTEMPTS = 3
LOOKBACK = timedelta(hours=24)
REQUEST_TIMEOUT = 300.0


def _destination(target_branch: str) -> tuple[str, str] | None:
    if target_branch == "master":
        return "stable", "stable"
    if target_branch == "beta":
        return "beta", "beta"
    if target_branch.startswith("branch/") and target_branch[7:]:
        return "stable", target_branch[7:]
    return None


def build_assessment_request(pipeline: Pipeline) -> dict[str, Any] | None:
    params = pipeline.params or {}
    git_repo = params.get("repo")
    head = normalize_git_oid(params.get("sha"))
    target_branch = params.get("pr_target_branch")
    if (
        pipeline.flat_manager_repo != "test"
        or pipeline.build_id is None
        or not isinstance(git_repo, str)
        or not git_repo
        or head is None
        or not isinstance(target_branch, str)
    ):
        return None
    raw_pr_number = params.get("pr_number")
    if not isinstance(raw_pr_number, (str, int)):
        return None
    try:
        pr_number = int(raw_pr_number)
    except ValueError:
        return None
    if pr_number <= 0:
        return None
    destination = _destination(target_branch)
    if destination is None:
        return None
    channel, flatpak_branch = destination

    request: dict[str, Any] = {
        "pipeline_id": str(pipeline.id),
        "build_id": pipeline.build_id,
        "forge_instance": "github.com",
        "source_repository": git_repo,
        "pull_request_number": pr_number,
        "pull_request_url": f"https://github.com/{git_repo}/pull/{pr_number}",
        "pull_request_head_revision": head,
        "built_revision": normalize_git_oid(params.get("verified_sha")) or head,
        "target_git_branch": target_branch,
        "candidate_kind": "head",
        "app_id": pipeline.app_id,
        "destination_repo": pipeline.flat_manager_repo,
        "destination_channel": channel,
        "flatpak_branch": flatpak_branch,
        "matrix_succeeded": True,
    }
    base = normalize_git_oid(params.get("base_sha"))
    if base is not None:
        request["base_revision"] = base
    return request


def _token() -> str:
    assert settings.permission_assessment_secret is not None
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": "permission-assessment",
            "scope": "assess",
            "iat": now,
            "exp": now + timedelta(minutes=10),
        },
        settings.permission_assessment_secret,
        algorithm="HS256",
    )


def _finished(pipeline: Pipeline) -> bool:
    state = (pipeline.provider_data or {}).get(PROVIDER_DATA_KEY) or {}
    return (
        state.get("status") in ("submitted", "skipped")
        or state.get("attempts", 0) >= MAX_ATTEMPTS
    )


async def _assess(
    pipeline: Pipeline, request: dict[str, Any], client: httpx.AsyncClient
) -> dict[str, Any]:
    run_info = JobMonitor._get_github_run_info(pipeline)
    if run_info is None:
        raise ValueError("Pipeline lacks GitHub Actions run information")
    owner, repo, run_id = run_info
    arches = await get_build_job_arches(run_id, owner=owner, repo=repo)
    if not arches:
        raise ValueError("No build architectures found for workflow run")

    assert settings.permission_assessment_url is not None
    response = await client.post(
        settings.permission_assessment_url,
        json={**request, "expected_arches": sorted(set(arches))},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    response.raise_for_status()
    body = response.json()
    return {
        "status": "submitted",
        "assessment_id": body["assessment_id"],
        "outcome": body["outcome"],
        "snapshot_fingerprint": body.get("snapshot_fingerprint"),
        "error_code": body.get("error_code"),
    }


async def submit_pending_assessments() -> dict[str, Any]:
    if (
        not settings.permission_assessment_url
        or not settings.permission_assessment_secret
    ):
        return {"status": "disabled"}

    counts = {"submitted": 0, "skipped": 0, "failed": 0}
    cutoff = datetime.now(UTC) - LOOKBACK
    async with get_db() as db:
        result = await db.execute(
            select(Pipeline)
            .where(
                Pipeline.status == PipelineStatus.COMMITTED,
                Pipeline.flat_manager_repo == "test",
                Pipeline.build_id.isnot(None),
                Pipeline.created_at > cutoff,
            )
            .order_by(Pipeline.created_at)
        )
        pipelines = [
            pipeline
            for pipeline in result.scalars()
            if (pipeline.params or {}).get("pr_number") and not _finished(pipeline)
        ]

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            for pipeline in pipelines:
                provider_data = dict(pipeline.provider_data or {})
                state = dict(provider_data.get(PROVIDER_DATA_KEY) or {})
                request = build_assessment_request(pipeline)
                if request is None:
                    state = {"status": "skipped"}
                else:
                    attempts = state.get("attempts", 0) + 1
                    try:
                        state = {
                            **await _assess(pipeline, request, client),
                            "attempts": attempts,
                        }
                    except Exception as e:
                        logger.exception(
                            "Failed to submit permission assessment",
                            pipeline_id=str(pipeline.id),
                            app_id=pipeline.app_id,
                            build_id=pipeline.build_id,
                            attempts=attempts,
                        )
                        state = {
                            "status": "failed",
                            "attempts": attempts,
                            "last_error": str(e),
                        }
                    else:
                        logger.info(
                            "Submitted permission assessment",
                            pipeline_id=str(pipeline.id),
                            app_id=pipeline.app_id,
                            build_id=pipeline.build_id,
                            assessment_id=state["assessment_id"],
                            outcome=state["outcome"],
                            error_code=state["error_code"],
                        )
                state["updated_at"] = datetime.now(UTC).isoformat()
                provider_data[PROVIDER_DATA_KEY] = state
                pipeline.provider_data = provider_data
                await db.commit()
                counts[state["status"]] += 1

    return {"status": "completed", **counts}
