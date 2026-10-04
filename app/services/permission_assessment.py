from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2 as httpx
import jwt
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Pipeline, PipelineStatus
from app.services.job_monitor import JobMonitor
from app.utils.flat_manager import PublishedState, get_flat_manager_client
from app.utils.github import (
    get_build_job_arches,
    get_github_client,
    get_pull_request,
    normalize_git_oid,
    update_commit_status,
    upsert_pr_comment,
)

logger = structlog.get_logger(__name__)

PROVIDER_DATA_KEY = "permission_assessment"
MAX_ATTEMPTS = 3
LOOKBACK = timedelta(hours=24)
REQUEST_TIMEOUT = 300.0
STATUS_LOOKBACK = timedelta(days=30)
STATUS_CONTEXT = "flathub/permissions"
COMMENT_MARKER = "<!-- flathub-permission-review -->"
STATUSES = {
    "accepted": ("success", "Permissions accepted"),
    "pending": ("pending", "Permission review pending on Flathub"),
    "rejected": ("failure", "Permissions rejected on Flathub"),
    "error": ("error", "Permission assessment failed"),
    "not_applicable": ("success", "No application permissions to review"),
    "target_changed": ("pending", "Target branch changed; reassessing"),
}
ASSESSMENT_FIELDS = [
    "assessment_id",
    "outcome",
    "snapshot_fingerprint",
    "error_code",
    "linked_assessment_id",
    "linked_fingerprint_match",
    "review_url",
]


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
    if reports := (pipeline.provider_data or {}).get("linter_reports"):
        request["linter_reports"] = reports
    return request


def build_push_assessment_request(pipeline: Pipeline) -> dict[str, Any] | None:
    params = pipeline.params or {}
    git_repo = params.get("repo")
    sha = normalize_git_oid(params.get("sha"))
    ref = params.get("ref")
    if (
        pipeline.flat_manager_repo not in ("stable", "beta")
        or pipeline.build_id is None
        or params.get("push") != "true"
        or not isinstance(git_repo, str)
        or not git_repo
        or sha is None
        or not isinstance(ref, str)
        or not ref.startswith("refs/heads/")
    ):
        return None
    target_branch = ref.removeprefix("refs/heads/")
    destination = _destination(target_branch)
    if destination is None or destination[0] != pipeline.flat_manager_repo:
        return None
    channel, flatpak_branch = destination

    request: dict[str, Any] = {
        "pipeline_id": str(pipeline.id),
        "build_id": pipeline.build_id,
        "forge_instance": "github.com",
        "source_repository": git_repo,
        "built_revision": sha,
        "target_git_branch": target_branch,
        "candidate_kind": "push",
        "app_id": pipeline.app_id,
        "destination_repo": pipeline.flat_manager_repo,
        "destination_channel": channel,
        "flatpak_branch": flatpak_branch,
        "matrix_succeeded": True,
    }
    base = normalize_git_oid(params.get("base_sha"))
    if base is not None:
        request["base_revision"] = base
    if reports := (pipeline.provider_data or {}).get("linter_reports"):
        request["linter_reports"] = reports
    return request


async def _merged_pull_request(
    git_repo: str, sha: str, target_branch: str
) -> dict[str, Any]:
    response = await get_github_client().request(
        "get",
        f"https://api.github.com/repos/{git_repo}/commits/{sha}/pulls",
        context={"repo": git_repo, "sha": sha},
    )
    if response is None:
        raise ValueError("Failed to look up pull requests for pushed commit")
    pulls = response.json()
    merged = [
        pull
        for pull in (pulls if isinstance(pulls, list) else [])
        if isinstance(pull, dict)
        and pull.get("merged_at")
        and (pull.get("base") or {}).get("ref") == target_branch
        and normalize_git_oid(pull.get("merge_commit_sha")) == sha
    ]
    if len(merged) != 1:
        return {}
    number = merged[0].get("number")
    head = normalize_git_oid((merged[0].get("head") or {}).get("sha"))
    if type(number) is not int or number <= 0 or head is None:
        return {}
    return {
        "pull_request_number": number,
        "pull_request_url": f"https://github.com/{git_repo}/pull/{number}",
        "pull_request_head_revision": head,
    }


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


async def _skip_reason(pipeline: Pipeline) -> str | None:
    assert pipeline.build_id is not None
    info = await get_flat_manager_client().get_build_info(pipeline.build_id)
    refs = [item["ref_name"] for item in info["build_refs"]]
    if refs and not any(ref.startswith("app/") for ref in refs):
        return "no_app_ref"
    if pipeline.flat_manager_repo != "test":
        published_state = PublishedState(info["build"]["published_state"])
        if published_state != PublishedState.UNPUBLISHED:
            return f"published_state_{published_state.name.lower()}"
    return None


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
    return _parse(
        await client.post(
            settings.permission_assessment_url,
            json={**request, "expected_arches": sorted(set(arches))},
            headers={"Authorization": f"Bearer {_token()}"},
        )
    )


def _parse(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    body = response.json()
    return {"status": "submitted", **{k: body.get(k) for k in ASSESSMENT_FIELDS}}


def _is_candidate(pipeline: Pipeline) -> bool:
    params = pipeline.params or {}
    if pipeline.flat_manager_repo == "test":
        return bool(params.get("pr_number"))
    return params.get("push") == "true"


def _enabled(target_branch: Any) -> bool:
    destination = _destination(target_branch) if target_branch else None
    return bool(destination and destination[0] in settings.permission_status_channels)


async def _report(
    request: dict[str, Any], state: dict[str, Any], outcome: str
) -> dict[str, Any]:
    url = state.get("review_url")
    if (
        request["candidate_kind"] != "head"
        or not _enabled(request["target_git_branch"])
        or state.get("reported") == [outcome, url]
    ):
        return state
    status, description = STATUSES[outcome]
    repo, head = request["source_repository"], request["pull_request_head_revision"]
    ok = await update_commit_status(
        head, status, repo, url, description, STATUS_CONTEXT
    )
    if ok and outcome in ("accepted", "pending", "rejected", "error"):
        body = [COMMENT_MARKER, f"**Flathub permission review: {description}**", ""]
        body += [f"Commit: `{head[:12]}`"] + ([f"Review: {url}"] if url else [])
        ok = await upsert_pr_comment(
            repo,
            request["pull_request_number"],
            COMMENT_MARKER,
            "\n".join(body),
            outcome != "accepted",
        )
    return {**state, "reported": [outcome, url]} if ok else state


async def notify_target_changed(payload: dict[str, Any]) -> None:
    pr = payload["pull_request"]
    old = payload["changes"]["base"].get("ref", {}).get("from")
    if _enabled(old) or _enabled(pr["base"]["ref"]):
        state, description = STATUSES["target_changed"]
        repo = payload["repository"]["full_name"]
        await update_commit_status(
            pr["head"]["sha"], state, repo, None, description, STATUS_CONTEXT
        )


async def _store(db: AsyncSession, pipeline: Pipeline, state: dict[str, Any]) -> None:
    await db.refresh(pipeline, ["provider_data"], with_for_update=True)
    data = pipeline.provider_data or {}
    pipeline.provider_data = {**data, PROVIDER_DATA_KEY: state}
    await db.commit()


async def _refresh(
    db: AsyncSession, pipeline: Pipeline, client: httpx.AsyncClient
) -> None:
    state = (pipeline.provider_data or {}).get(PROVIDER_DATA_KEY) or {}
    request = build_assessment_request(pipeline)
    if (
        state.get("status") != "submitted"
        or state.get("closed")
        or request is None
        or not _enabled(request["target_git_branch"])
    ):
        return
    pr = await get_pull_request(
        request["source_repository"], request["pull_request_number"]
    )
    if pr is None:
        return
    if pr["state"] != "open":
        new = {**state, "closed": True}
    elif pr["head"]["sha"] != request["pull_request_head_revision"]:
        return
    elif pr["base"]["ref"] != request["target_git_branch"]:
        new = await _report(request, state, "target_changed")
    else:
        base = pr["base"]["sha"]
        recorded = state.get("base_revision", request.get("base_revision"))
        if recorded and base != recorded:
            request = {**request, "base_revision": base}
            new = {**state, **await _assess(pipeline, request, client)}
            new["base_revision"] = base
        else:
            assert settings.permission_assessment_url is not None
            url = settings.permission_assessment_url.rstrip("/").removesuffix("/assess")
            response = await client.get(
                f"{url}/{state['assessment_id']}",
                headers={"Authorization": f"Bearer {_token()}"},
            )
            new = {**state, **_parse(response)}
        new = await _report(request, new, new["outcome"])
    if new != state:
        await _store(db, pipeline, new)


async def _refresh_statuses(db: AsyncSession, client: httpx.AsyncClient) -> None:
    result = await db.execute(
        select(Pipeline)
        .where(
            Pipeline.flat_manager_repo == "test",
            Pipeline.created_at > datetime.now(UTC) - STATUS_LOOKBACK,
        )
        .order_by(Pipeline.created_at)
    )
    latest = {
        (p.params.get("repo"), p.params["pr_number"]): p
        for p in result.scalars()
        if (p.params or {}).get("pr_number")
    }
    for pipeline in latest.values():
        try:
            await _refresh(db, pipeline, client)
        except Exception:
            logger.exception("Failed to refresh status", pipeline_id=str(pipeline.id))


async def submit_pending_assessments() -> dict[str, Any]:
    if (
        not settings.permission_assessment_url
        or not settings.permission_assessment_secret
    ):
        return {"status": "disabled"}

    counts = {"submitted": 0, "skipped": 0, "failed": 0}
    cutoff = datetime.now(UTC) - LOOKBACK
    repos = ["test"]
    if settings.ff_permission_assessment_push:
        repos += ["stable", "beta"]
    async with get_db() as db:
        result = await db.execute(
            select(Pipeline)
            .where(
                Pipeline.status == PipelineStatus.COMMITTED,
                Pipeline.flat_manager_repo.in_(repos),
                Pipeline.build_id.isnot(None),
                Pipeline.created_at > cutoff,
            )
            .order_by(Pipeline.created_at)
        )
        pipelines = [
            pipeline
            for pipeline in result.scalars()
            if _is_candidate(pipeline) and not _finished(pipeline)
        ]

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            for pipeline in pipelines:
                provider_data = dict(pipeline.provider_data or {})
                state = dict(provider_data.get(PROVIDER_DATA_KEY) or {})
                request = (
                    build_assessment_request(pipeline)
                    if pipeline.flat_manager_repo == "test"
                    else build_push_assessment_request(pipeline)
                )
                if request is None:
                    state = {"status": "skipped"}
                else:
                    attempts = state.get("attempts", 0) + 1
                    try:
                        reason = await _skip_reason(pipeline)
                        if reason is not None:
                            state = {"status": "skipped", "reason": reason}
                            if reason == "no_app_ref":
                                state = await _report(request, state, "not_applicable")
                        else:
                            if request["candidate_kind"] == "push":
                                request = {
                                    **request,
                                    **await _merged_pull_request(
                                        request["source_repository"],
                                        request["built_revision"],
                                        request["target_git_branch"],
                                    ),
                                }
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
                        if attempts >= MAX_ATTEMPTS:
                            state = await _report(request, state, "error")
                    else:
                        if state["status"] == "skipped":
                            logger.info(
                                "Skipped permission assessment",
                                pipeline_id=str(pipeline.id),
                                app_id=pipeline.app_id,
                                build_id=pipeline.build_id,
                                reason=state["reason"],
                            )
                        else:
                            logger.info(
                                "Submitted permission assessment",
                                pipeline_id=str(pipeline.id),
                                app_id=pipeline.app_id,
                                build_id=pipeline.build_id,
                                assessment_id=state["assessment_id"],
                                outcome=state["outcome"],
                                error_code=state["error_code"],
                                candidate_kind=request["candidate_kind"],
                                pull_request_number=request.get("pull_request_number"),
                                linked_assessment_id=state["linked_assessment_id"],
                                linked_fingerprint_match=state[
                                    "linked_fingerprint_match"
                                ],
                            )
                state["updated_at"] = datetime.now(UTC).isoformat()
                await _store(db, pipeline, state)
                counts[state["status"]] += 1

            if settings.permission_status_channels:
                await _refresh_statuses(db, client)

    return {"status": "completed", **counts}
