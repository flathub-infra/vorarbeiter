import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx2 as httpx
import jwt
import structlog
from sqlalchemy import Select, String, cast, select, text
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
    post_pr_comment,
    update_commit_status,
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
    "rejected": ("failure", "Permission change rejected"),
    "error": ("error", "Permission assessment failed"),
    "not_applicable": ("success", "No application permissions to review"),
    "target_changed": ("pending", "Target branch changed; reassessing"),
}
COMMENTS = {
    "accepted": "The permissions in this pull request are approved.",
    "pending": "This pull request changes the app's permissions. "
    "A Flathub moderator will review the changes.",
    "rejected": "A Flathub moderator rejected the permission changes "
    "in this pull request.",
    "error": "Flathub could not check the permissions of this build.",
}
ASSESSMENT_FIELDS = [
    "assessment_id",
    "outcome",
    "snapshot_fingerprint",
    "error_code",
    "linked_assessment_id",
    "linked_fingerprint_match",
    "review_url",
    "differences",
    "decision_id",
    "decision_rationale",
]
REJECTION_HELP = (
    "<sub>If you disagree with this decision, comment"
    " `bot, ping admins` to notify the Flathub admins.</sub>"
)


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
    return bool(destination and destination[0] in settings.permission_status_repos)


def _values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [
            f"{key}={';'.join(item) if isinstance(item, list) else item}"
            for key, item in value.items()
        ]
    return value if isinstance(value, list) else [value]


def _diff(differences: list[dict[str, Any]]) -> list[str]:
    lines, header = ["```diff"], None
    for item in differences:
        path, before, after = item["path"], item["before"], item["after"]
        if not isinstance(before, list | dict) and not isinstance(after, list | dict):
            *path, key = path
            before = None if before is None else {key: before}
            after = None if after is None else {key: after}
        if path != header:
            lines.append(f"@@ {' / '.join(path)} @@")
            header = path
        before, after = _values(before), _values(after)
        lines += [f"- {value}" for value in before if value not in after]
        lines += [f"+ {value}" for value in after if value not in before]
    return [*lines, "```", ""]


def _report_marker(state: dict[str, Any], outcome: str) -> list[Any]:
    return [
        outcome,
        state.get("review_url"),
        state.get("decision_id"),
        (state.get("decision_rationale") or "").strip(),
    ]


async def _report(
    request: dict[str, Any], state: dict[str, Any], outcome: str
) -> dict[str, Any]:
    marker = _report_marker(state, outcome)
    if (
        request["candidate_kind"] != "head"
        or not _enabled(request["target_git_branch"])
        or state.get("reported") == marker
    ):
        return state
    status, description = STATUSES[outcome]
    repo, head = request["source_repository"], request["pull_request_head_revision"]
    ok = await update_commit_status(
        head, status, repo, None, description, STATUS_CONTEXT
    )
    if ok and outcome in COMMENTS:
        body = [COMMENT_MARKER, COMMENTS[outcome], ""]
        if rationale := marker[3]:
            body += [f"> {line}" for line in rationale.splitlines()] + [""]
        if outcome == "rejected":
            body += [REJECTION_HELP, ""]
        if outcome == "pending" and state.get("differences"):
            body += _diff(state["differences"])
            body += [
                "Explain why these changes are needed; moderators may reject it.",
                "",
            ]
        body += ["This check does not block merging."]
        ok = await post_pr_comment(
            repo,
            request["pull_request_number"],
            COMMENT_MARKER,
            "\n".join(body),
            outcome != "accepted" or state.get("decision_id") is not None,
        )
    return {**state, "reported": marker} if ok else state


async def notify_target_changed(payload: dict[str, Any]) -> None:
    pr = payload["pull_request"]
    old = payload["changes"]["base"].get("ref", {}).get("from")
    if _enabled(old) or _enabled(pr["base"]["ref"]):
        state, description = STATUSES["target_changed"]
        repo = payload["repository"]["full_name"]
        await update_commit_status(
            pr["head"]["sha"], state, repo, None, description, STATUS_CONTEXT
        )


async def _lock_assessment(db: AsyncSession, pipeline: Pipeline) -> None:
    if db.get_bind().dialect.name != "sqlite":
        request = build_assessment_request(pipeline)
        name = (
            f"permission-assessment:{request['source_repository']}:"
            f"{request['pull_request_number']}"
            if request is not None
            else f"permission-assessment:{pipeline.id}"
        )
        key = int.from_bytes(
            hashlib.sha256(name.encode()).digest()[:8], "big", signed=True
        )
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    await db.refresh(pipeline)


async def _store(db: AsyncSession, pipeline: Pipeline, state: dict[str, Any]) -> None:
    await db.refresh(pipeline, ["provider_data"], with_for_update=True)
    data = pipeline.provider_data or {}
    pipeline.provider_data = {**data, PROVIDER_DATA_KEY: state}
    await db.commit()


def _submitted() -> Select[tuple[Pipeline]]:
    return select(Pipeline).where(
        Pipeline.flat_manager_repo == "test",
        Pipeline.provider_data[PROVIDER_DATA_KEY]["status"].as_string() == "submitted",
    )


def _latest_submitted(
    source_repository: str, pull_request_number: int
) -> Select[tuple[Pipeline]]:
    return (
        _submitted()
        .where(
            Pipeline.params["repo"].as_string() == source_repository,
            cast(Pipeline.params["pr_number"].as_string(), String)
            == str(pull_request_number),
        )
        .order_by(Pipeline.created_at.desc(), Pipeline.id.desc())
        .limit(1)
    )


async def _refresh(
    db: AsyncSession, pipeline: Pipeline, client: httpx.AsyncClient
) -> None:
    await _lock_assessment(db, pipeline)
    state = (pipeline.provider_data or {}).get(PROVIDER_DATA_KEY) or {}
    request = build_assessment_request(pipeline)
    if (
        state.get("status") != "submitted"
        or state.get("closed")
        or request is None
        or not _enabled(request["target_git_branch"])
    ):
        return
    latest = await db.scalar(
        _latest_submitted(
            request["source_repository"], request["pull_request_number"]
        ).with_only_columns(Pipeline.id)
    )
    if latest != pipeline.id:
        return
    pr = await get_pull_request(
        request["source_repository"], request["pull_request_number"]
    )
    if pr is None:
        raise RuntimeError("Failed to fetch permission pull request")
    outcome = None
    if pr["state"] != "open":
        new = {**state, "closed": True}
    elif pr["head"]["sha"] != request["pull_request_head_revision"]:
        return
    elif pr["base"]["ref"] != request["target_git_branch"]:
        new, outcome = state, "target_changed"
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
        outcome = new["outcome"]
    try:
        if outcome is not None:
            new = await _report(request, new, outcome)
    finally:
        if new != state:
            await _store(db, pipeline, new)
    if outcome is not None and new.get("reported") != _report_marker(new, outcome):
        raise RuntimeError("Failed to report permission assessment")


async def _refresh_statuses(client: httpx.AsyncClient) -> None:
    async with get_db() as db:
        result = await db.execute(
            _submitted()
            .where(Pipeline.created_at > datetime.now(UTC) - STATUS_LOOKBACK)
            .order_by(Pipeline.created_at, Pipeline.id)
        )
        latest = {
            (p.params.get("repo"), str(p.params["pr_number"])): p.id
            for p in result.scalars()
            if (p.params or {}).get("pr_number")
        }
    for pipeline_id in latest.values():
        try:
            async with get_db() as db:
                pipeline = await db.get(Pipeline, pipeline_id)
                if pipeline is not None:
                    await _refresh(db, pipeline, client)
        except Exception:
            logger.exception("Failed to refresh status", pipeline_id=str(pipeline_id))


async def refresh_permission_review(
    source_repository: str, pull_request_number: int
) -> None:
    async with get_db() as db:
        pipeline = await db.scalar(
            _latest_submitted(source_repository, pull_request_number)
        )
        if pipeline is None:
            return
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            await _refresh(db, pipeline, client)


def _configured() -> bool:
    return bool(
        settings.permission_assessment_url and settings.permission_assessment_secret
    )


def _repos() -> list[str]:
    repos = ["test"]
    if settings.ff_permission_assessment_push:
        repos += ["stable", "beta"]
    return repos


async def _submit(
    db: AsyncSession, pipeline: Pipeline, client: httpx.AsyncClient
) -> str | None:
    await _lock_assessment(db, pipeline)
    if (
        pipeline.status != PipelineStatus.COMMITTED
        or pipeline.flat_manager_repo not in _repos()
        or pipeline.build_id is None
        or JobMonitor._as_utc(pipeline.created_at) <= datetime.now(UTC) - LOOKBACK
        or not _is_candidate(pipeline)
        or _finished(pipeline)
    ):
        return None

    state = dict((pipeline.provider_data or {}).get(PROVIDER_DATA_KEY) or {})
    request = (
        build_assessment_request(pipeline)
        if pipeline.flat_manager_repo == "test"
        else build_push_assessment_request(pipeline)
    )
    report = None
    if request is None:
        state = {"status": "skipped"}
    else:
        attempts = state.get("attempts", 0) + 1
        try:
            reason = await _skip_reason(pipeline)
            if reason is not None:
                state = {"status": "skipped", "reason": reason}
                if reason == "no_app_ref":
                    report = "not_applicable"
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
                report = "error"
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
                    linked_fingerprint_match=state["linked_fingerprint_match"],
                )
    state["updated_at"] = datetime.now(UTC).isoformat()
    await _store(db, pipeline, state)

    if request is not None and report is not None:
        try:
            await _lock_assessment(db, pipeline)
            current = (pipeline.provider_data or {}).get(PROVIDER_DATA_KEY) or {}
            reported = await _report(request, current, report)
            if reported != current:
                await _store(db, pipeline, reported)
        except Exception:
            logger.exception(
                "Failed to report permission assessment",
                pipeline_id=str(pipeline.id),
            )
            await db.rollback()
    return state["status"]


async def submit_pipeline_assessment(pipeline_id: UUID) -> None:
    if not _configured():
        return
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        async with get_db() as db:
            pipeline = await db.get(Pipeline, pipeline_id)
            if pipeline is None:
                return
            status = await _submit(db, pipeline, client)
        if status != "submitted":
            return
        try:
            async with get_db() as db:
                pipeline = await db.get(Pipeline, pipeline_id)
                assert pipeline is not None
                await _refresh(db, pipeline, client)
        except Exception:
            logger.exception(
                "Failed to refresh permission assessment",
                pipeline_id=str(pipeline_id),
            )


async def submit_pending_assessments() -> dict[str, Any]:
    if not _configured():
        return {"status": "disabled"}

    counts = {"submitted": 0, "skipped": 0, "failed": 0}
    cutoff = datetime.now(UTC) - LOOKBACK
    async with get_db() as db:
        result = await db.execute(
            select(Pipeline.id)
            .where(
                Pipeline.status == PipelineStatus.COMMITTED,
                Pipeline.flat_manager_repo.in_(_repos()),
                Pipeline.build_id.isnot(None),
                Pipeline.created_at > cutoff,
            )
            .order_by(Pipeline.created_at)
        )
        pipeline_ids = list(result.scalars())

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for pipeline_id in pipeline_ids:
            async with get_db() as db:
                pipeline = await db.get(Pipeline, pipeline_id)
                status = await _submit(db, pipeline, client) if pipeline else None
            if status:
                counts[status] += 1

        if settings.permission_status_repos:
            await _refresh_statuses(client)

    return {"status": "completed", **counts}
