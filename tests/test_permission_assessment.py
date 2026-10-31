import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import Pipeline, PipelineStatus
from app.services import permission_assessment
from app.services.job_monitor import JobMonitor
from app.services.permission_assessment import (
    MAX_ATTEMPTS,
    PROVIDER_DATA_KEY,
    build_assessment_request,
    build_push_assessment_request,
    submit_pending_assessments,
    submit_pipeline_assessment,
)
from app.utils.github import get_pull_request

HEAD = "a" * 40
BASE = "b" * 40
VERIFIED = "c" * 40
ASSESS_URL = "https://flathub.test/api/v2/moderation/permissions/assess"
SECRET = "permission-assessment-test-secret-0123456789"


def make_pipeline(**overrides) -> Pipeline:
    params = {
        "repo": "flathub/org.test.App",
        "ref": "refs/pull/42/head",
        "sha": HEAD,
        "base_sha": BASE,
        "pr_number": "42",
        "pr_target_branch": "master",
    }
    params.update(overrides.pop("params", {}))
    values = {
        "id": uuid.uuid4(),
        "app_id": "org.test.App",
        "status": PipelineStatus.COMMITTED,
        "flat_manager_repo": "test",
        "build_id": 1234,
        "commit_job_id": 5678,
        "provider_data": {
            "owner": "flathub-infra",
            "repo": "vorarbeiter",
            "run_id": 99,
        },
        "params": params,
    }
    values.update(overrides)
    return Pipeline(**values)


def test_build_request_for_stable_pull_request():
    pipeline = make_pipeline()

    assert build_assessment_request(pipeline) == {
        "pipeline_id": str(pipeline.id),
        "build_id": 1234,
        "forge_instance": "github.com",
        "source_repository": "flathub/org.test.App",
        "pull_request_number": 42,
        "pull_request_url": "https://github.com/flathub/org.test.App/pull/42",
        "pull_request_head_revision": HEAD,
        "built_revision": HEAD,
        "target_git_branch": "master",
        "base_revision": BASE,
        "candidate_kind": "head",
        "app_id": "org.test.App",
        "destination_repo": "test",
        "destination_channel": "stable",
        "flatpak_branch": "stable",
        "matrix_succeeded": True,
    }


@pytest.mark.parametrize(
    ("target", "channel", "branch"),
    [
        ("beta", "beta", "beta"),
        ("branch/24.08", "stable", "24.08"),
    ],
)
def test_build_request_destination(target, channel, branch):
    request = build_assessment_request(
        make_pipeline(params={"pr_target_branch": target})
    )

    assert request is not None
    assert request["destination_channel"] == channel
    assert request["flatpak_branch"] == branch
    assert request["target_git_branch"] == target


def test_build_request_prefers_verified_sha():
    request = build_assessment_request(make_pipeline(params={"verified_sha": VERIFIED}))

    assert request is not None
    assert request["built_revision"] == VERIFIED
    assert request["pull_request_head_revision"] == HEAD


def test_build_request_omits_missing_base():
    pipeline = make_pipeline()
    del pipeline.params["base_sha"]

    request = build_assessment_request(pipeline)

    assert request is not None
    assert "base_revision" not in request


@pytest.mark.parametrize(
    "overrides",
    [
        {"flat_manager_repo": "stable"},
        {"build_id": None},
        {"params": {"pr_number": None}},
        {"params": {"pr_number": "0"}},
        {"params": {"pr_number": "abc"}},
        {"params": {"sha": "not-a-sha"}},
        {"params": {"repo": None}},
        {"params": {"pr_target_branch": "feature"}},
        {"params": {"pr_target_branch": "branch/"}},
    ],
)
def test_build_request_ineligible(overrides):
    assert build_assessment_request(make_pipeline(**overrides)) is None


@pytest.fixture
def configured():
    with (
        patch.object(settings, "permission_assessment_url", ASSESS_URL),
        patch.object(settings, "permission_assessment_secret", SECRET),
    ):
        yield


def build_info(refs=("app/org.test.App/x86_64/stable",), published_state=0):
    return {
        "build": {"published_state": published_state},
        "build_refs": [{"ref_name": ref} for ref in refs],
    }


@pytest.fixture(autouse=True)
def flat_manager():
    client = AsyncMock()
    client.get_build_info.return_value = build_info()
    with patch(
        "app.services.permission_assessment.get_flat_manager_client",
        return_value=client,
    ):
        yield client


@pytest.fixture
def local_db(db_session_maker):
    @asynccontextmanager
    async def get_db(*, use_replica: bool = False):
        async with db_session_maker() as db:
            yield db

    with patch("app.services.permission_assessment.get_db", get_db):
        yield


def mock_http(handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        return real_client(transport=transport, **kwargs)

    return patch.object(permission_assessment.httpx, "AsyncClient", factory)


async def add(db_session_maker, *pipelines):
    async with db_session_maker() as db:
        db.add_all(pipelines)
        await db.commit()


async def load(db_session_maker, pipeline_id):
    async with db_session_maker() as db:
        return await db.scalar(select(Pipeline).where(Pipeline.id == pipeline_id))


@pytest.mark.asyncio
async def test_disabled_without_configuration():
    with patch.object(settings, "permission_assessment_url", None):
        assert await submit_pending_assessments() == {"status": "disabled"}


@pytest.mark.asyncio
async def test_submits_committed_pull_request_once(
    configured, local_db, db_session_maker
):
    pipeline = make_pipeline()
    await add(db_session_maker, pipeline)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "assessment_id": 7,
                "outcome": "pending",
                "snapshot_fingerprint": "f" * 64,
                "error_code": None,
            },
        )

    arches = AsyncMock(return_value=["x86_64", "aarch64", "x86_64"])
    with (
        mock_http(handler),
        patch("app.services.permission_assessment.get_build_job_arches", arches),
    ):
        first = await submit_pending_assessments()
        second = await submit_pending_assessments()

    assert first == {"status": "completed", "submitted": 1, "skipped": 0, "failed": 0}
    assert second == {"status": "completed", "submitted": 0, "skipped": 0, "failed": 0}
    assert len(requests) == 1
    arches.assert_awaited_once_with(99, owner="flathub-infra", repo="vorarbeiter")

    request = requests[0]
    assert str(request.url) == ASSESS_URL
    token = request.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert claims["sub"] == "permission-assessment"
    assert claims["scope"] == "assess"
    body = json.loads(request.content)
    assert body["expected_arches"] == ["aarch64", "x86_64"]
    assert body["build_id"] == 1234
    assert body["pipeline_id"] == str(pipeline.id)

    stored = await load(db_session_maker, pipeline.id)
    state = stored.provider_data[PROVIDER_DATA_KEY]
    assert state["status"] == "submitted"
    assert state["assessment_id"] == 7
    assert state["outcome"] == "pending"
    assert state["attempts"] == 1
    assert stored.provider_data["run_id"] == 99


@pytest.mark.asyncio
async def test_ignores_non_candidates(configured, local_db, db_session_maker):
    await add(
        db_session_maker,
        make_pipeline(flat_manager_repo="stable"),
        make_pipeline(status=PipelineStatus.SUCCEEDED),
        make_pipeline(params={"pr_number": None}),
        make_pipeline(build_id=None),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected request")

    with mock_http(handler):
        result = await submit_pending_assessments()

    assert result == {"status": "completed", "submitted": 0, "skipped": 0, "failed": 0}


@pytest.mark.asyncio
async def test_records_skipped_pipeline(configured, local_db, db_session_maker):
    pipeline = make_pipeline(params={"pr_target_branch": "feature"})
    await add(db_session_maker, pipeline)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected request")

    with mock_http(handler):
        first = await submit_pending_assessments()
        second = await submit_pending_assessments()

    assert first["skipped"] == 1
    assert second["skipped"] == 0
    stored = await load(db_session_maker, pipeline.id)
    assert stored.provider_data[PROVIDER_DATA_KEY]["status"] == "skipped"


@pytest.mark.asyncio
async def test_retries_failures_up_to_limit(configured, local_db, db_session_maker):
    pipeline = make_pipeline()
    await add(db_session_maker, pipeline)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, json={"detail": "flat_manager_unavailable"})

    with (
        mock_http(handler),
        patch(
            "app.services.permission_assessment.get_build_job_arches",
            AsyncMock(return_value=["x86_64"]),
        ),
    ):
        for _ in range(MAX_ATTEMPTS + 1):
            await submit_pending_assessments()

    assert calls == MAX_ATTEMPTS
    stored = await load(db_session_maker, pipeline.id)
    state = stored.provider_data[PROVIDER_DATA_KEY]
    assert state["status"] == "failed"
    assert state["attempts"] == MAX_ATTEMPTS
    assert "502" in state["last_error"]


@pytest.mark.asyncio
async def test_fails_without_build_arches(configured, local_db, db_session_maker):
    pipeline = make_pipeline()
    await add(db_session_maker, pipeline)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected request")

    with (
        mock_http(handler),
        patch(
            "app.services.permission_assessment.get_build_job_arches",
            AsyncMock(return_value=[]),
        ),
    ):
        result = await submit_pending_assessments()

    assert result["failed"] == 1
    stored = await load(db_session_maker, pipeline.id)
    assert stored.provider_data[PROVIDER_DATA_KEY]["attempts"] == 1


@pytest.mark.asyncio
async def test_skips_build_without_app_ref(
    configured, local_db, db_session_maker, flat_manager
):
    pipeline = make_pipeline()
    await add(db_session_maker, pipeline)
    flat_manager.get_build_info.return_value = build_info(
        refs=(
            "runtime/org.test.App/x86_64/stable",
            "runtime/org.test.App.Sources/x86_64/stable",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected request")

    with mock_http(handler):
        first = await submit_pending_assessments()
        second = await submit_pending_assessments()

    assert first == {"status": "completed", "submitted": 0, "skipped": 1, "failed": 0}
    assert second == {"status": "completed", "submitted": 0, "skipped": 0, "failed": 0}
    flat_manager.get_build_info.assert_awaited_once_with(1234)
    stored = await load(db_session_maker, pipeline.id)
    state = stored.provider_data[PROVIDER_DATA_KEY]
    assert state["status"] == "skipped"
    assert state["reason"] == "no_app_ref"


PUSHED = "d" * 40
PR_HEAD = "e" * 40


def make_push_pipeline(ref="refs/heads/master", **overrides) -> Pipeline:
    pipeline = make_pipeline(**{"flat_manager_repo": "stable", **overrides})
    pipeline.params = {
        "repo": "flathub/org.test.App",
        "ref": ref,
        "push": "true",
        "sha": PUSHED,
        "base_sha": BASE,
    }
    return pipeline


def test_build_push_request_for_stable_merge():
    pipeline = make_push_pipeline()

    assert build_push_assessment_request(pipeline) == {
        "pipeline_id": str(pipeline.id),
        "build_id": 1234,
        "forge_instance": "github.com",
        "source_repository": "flathub/org.test.App",
        "built_revision": PUSHED,
        "target_git_branch": "master",
        "base_revision": BASE,
        "candidate_kind": "push",
        "app_id": "org.test.App",
        "destination_repo": "stable",
        "destination_channel": "stable",
        "flatpak_branch": "stable",
        "matrix_succeeded": True,
    }


@pytest.mark.parametrize(
    ("ref", "repo", "channel", "branch"),
    [
        ("refs/heads/beta", "beta", "beta", "beta"),
        ("refs/heads/branch/24.08", "stable", "stable", "24.08"),
    ],
)
def test_build_push_request_destination(ref, repo, channel, branch):
    request = build_push_assessment_request(
        make_push_pipeline(ref=ref, flat_manager_repo=repo)
    )

    assert request is not None
    assert request["destination_repo"] == repo
    assert request["destination_channel"] == channel
    assert request["flatpak_branch"] == branch


@pytest.mark.parametrize(
    ("ref", "repo", "params"),
    [
        ("refs/heads/master", "test", {}),
        ("refs/heads/master", "stable", {"push": None}),
        ("refs/heads/master", "stable", {"sha": "bad"}),
        ("refs/heads/master", "stable", {"repo": None}),
        ("refs/tags/v1", "stable", {}),
        ("refs/heads/feature", "stable", {}),
        ("refs/heads/beta", "stable", {}),
        ("refs/heads/master", "beta", {}),
    ],
)
def test_build_push_request_ineligible(ref, repo, params):
    pipeline = make_push_pipeline(ref=ref)
    pipeline.flat_manager_repo = repo
    pipeline.params = {**pipeline.params, **params}

    assert build_push_assessment_request(pipeline) is None


def pull(number=7, merged=True, base="master", merge_sha=PUSHED, head=PR_HEAD):
    return {
        "number": number,
        "merged_at": "2026-10-01T20:00:00Z" if merged else None,
        "base": {"ref": base},
        "merge_commit_sha": merge_sha,
        "head": {"sha": head},
    }


def github_returning(payload):
    response = httpx.Response(200, json=payload)
    client = AsyncMock()
    client.request = AsyncMock(return_value=response)
    return patch(
        "app.services.permission_assessment.get_github_client", return_value=client
    )


@pytest.mark.asyncio
async def test_merged_pull_request_links_unique_merge():
    with github_returning([pull(), pull(number=8, merged=False)]):
        linked = await permission_assessment._merged_pull_request(
            "flathub/org.test.App", PUSHED, "master"
        )

    assert linked == {
        "pull_request_number": 7,
        "pull_request_url": "https://github.com/flathub/org.test.App/pull/7",
        "pull_request_head_revision": PR_HEAD,
    }


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [pull(merged=False)],
        [pull(base="beta")],
        [pull(merge_sha="f" * 40)],
        [pull(head="bad")],
        [pull(), pull(number=8)],
        {"message": "Not Found"},
    ],
)
@pytest.mark.asyncio
async def test_merged_pull_request_without_unique_merge(payload):
    with github_returning(payload):
        linked = await permission_assessment._merged_pull_request(
            "flathub/org.test.App", PUSHED, "master"
        )

    assert linked == {}


@pytest.mark.asyncio
async def test_merged_pull_request_lookup_failure_raises():
    client = AsyncMock()
    client.request = AsyncMock(return_value=None)
    with (
        patch(
            "app.services.permission_assessment.get_github_client",
            return_value=client,
        ),
        pytest.raises(ValueError),
    ):
        await permission_assessment._merged_pull_request(
            "flathub/org.test.App", PUSHED, "master"
        )


@pytest.mark.asyncio
async def test_push_builds_ignored_without_flag(configured, local_db, db_session_maker):
    await add(db_session_maker, make_push_pipeline())

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected request")

    with (
        patch.object(settings, "ff_permission_assessment_push", False),
        mock_http(handler),
    ):
        result = await submit_pending_assessments()

    assert result == {"status": "completed", "submitted": 0, "skipped": 0, "failed": 0}


@pytest.mark.parametrize(
    "status", [PipelineStatus.PUBLISHING, PipelineStatus.PUBLISHED]
)
@pytest.mark.asyncio
async def test_ignores_push_builds_after_publication_started(
    configured, local_db, db_session_maker, flat_manager, status
):
    await add(db_session_maker, make_push_pipeline(status=status))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected request")

    with (
        patch.object(settings, "ff_permission_assessment_push", True),
        mock_http(handler),
    ):
        result = await submit_pending_assessments()

    assert result == {"status": "completed", "submitted": 0, "skipped": 0, "failed": 0}
    flat_manager.get_build_info.assert_not_awaited()


@pytest.mark.parametrize(
    ("published_state", "reason"),
    [(1, "published_state_publishing"), (2, "published_state_published")],
)
@pytest.mark.asyncio
async def test_skips_committed_push_build_published_in_flat_manager(
    configured, local_db, db_session_maker, flat_manager, published_state, reason
):
    pipeline = make_push_pipeline()
    await add(db_session_maker, pipeline)
    flat_manager.get_build_info.return_value = build_info(
        published_state=published_state
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected request")

    with (
        patch.object(settings, "ff_permission_assessment_push", True),
        mock_http(handler),
        patch(
            "app.services.permission_assessment._merged_pull_request",
            AsyncMock(side_effect=AssertionError("unexpected lookup")),
        ),
    ):
        result = await submit_pending_assessments()

    assert result == {"status": "completed", "submitted": 0, "skipped": 1, "failed": 0}
    stored = await load(db_session_maker, pipeline.id)
    assert stored.provider_data[PROVIDER_DATA_KEY] == {
        "status": "skipped",
        "reason": reason,
        "updated_at": stored.provider_data[PROVIDER_DATA_KEY]["updated_at"],
    }


@pytest.mark.asyncio
async def test_submits_committed_push_builds_with_linked_pull_request(
    configured, local_db, db_session_maker
):
    linked = make_push_pipeline()
    direct = make_push_pipeline()
    direct.params = {**direct.params, "sha": "1" * 40}
    running = make_push_pipeline(status=PipelineStatus.RUNNING)
    await add(db_session_maker, linked, direct, running)
    bodies = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies[body["pipeline_id"]] = body
        return httpx.Response(
            200,
            json={
                "assessment_id": len(bodies),
                "outcome": "pending",
                "snapshot_fingerprint": "f" * 64,
                "error_code": None,
                "linked_assessment_id": 3 if "pull_request_number" in body else None,
                "linked_fingerprint_match": True
                if "pull_request_number" in body
                else None,
            },
        )

    async def merged(git_repo, sha, target_branch):
        assert (git_repo, target_branch) == ("flathub/org.test.App", "master")
        return (
            {
                "pull_request_number": 7,
                "pull_request_url": "https://github.com/flathub/org.test.App/pull/7",
                "pull_request_head_revision": PR_HEAD,
            }
            if sha == PUSHED
            else {}
        )

    with (
        patch.object(settings, "ff_permission_assessment_push", True),
        mock_http(handler),
        patch(
            "app.services.permission_assessment.get_build_job_arches",
            AsyncMock(return_value=["x86_64"]),
        ),
        patch("app.services.permission_assessment._merged_pull_request", merged),
    ):
        result = await submit_pending_assessments()

    assert result == {"status": "completed", "submitted": 2, "skipped": 0, "failed": 0}
    assert set(bodies) == {str(linked.id), str(direct.id)}
    linked_body = bodies[str(linked.id)]
    assert linked_body["candidate_kind"] == "push"
    assert linked_body["pull_request_number"] == 7
    assert linked_body["pull_request_head_revision"] == PR_HEAD
    assert linked_body["built_revision"] == PUSHED
    assert "pull_request_number" not in bodies[str(direct.id)]

    stored = await load(db_session_maker, linked.id)
    state = stored.provider_data[PROVIDER_DATA_KEY]
    assert state["status"] == "submitted"
    assert state["linked_assessment_id"] == 3
    assert state["linked_fingerprint_match"] is True


@pytest.mark.asyncio
async def test_keeps_provider_data_written_during_run(
    configured, local_db, db_session_maker
):
    pipeline = make_pipeline()
    await add(db_session_maker, pipeline)

    async def assess(pipeline_, request, client):
        async with db_session_maker() as db:
            row = await db.get(Pipeline, pipeline.id)
            row.provider_data = {**row.provider_data, "reported_flat_manager_jobs": 1}
            await db.commit()
        return {
            "status": "submitted",
            "assessment_id": 9,
            "outcome": "pending",
            "snapshot_fingerprint": None,
            "error_code": None,
            "linked_assessment_id": None,
            "linked_fingerprint_match": None,
        }

    with patch("app.services.permission_assessment._assess", assess):
        await submit_pending_assessments()

    stored = await load(db_session_maker, pipeline.id)
    assert stored.provider_data["reported_flat_manager_jobs"] == 1
    assert stored.provider_data[PROVIDER_DATA_KEY]["assessment_id"] == 9


NEW_BASE = "d" * 40
REVIEW_URL = "https://flathub.test/review/7"


@pytest.fixture
def gh():
    pr = {
        "state": "open",
        "head": {"sha": HEAD},
        "base": {"ref": "master", "sha": BASE},
    }
    get_pr = AsyncMock(return_value=pr)
    status, comment = AsyncMock(return_value=True), AsyncMock(return_value=True)
    with (
        patch.object(settings, "permission_status_repos", ["stable"]),
        patch.object(permission_assessment, "get_pull_request", get_pr),
        patch.object(permission_assessment, "update_commit_status", status),
        patch.object(permission_assessment, "post_pr_comment", comment),
    ):
        yield SimpleNamespace(pr=pr, get_pr=get_pr, status=status, comment=comment)


def submitted(**state):
    state = {"status": "submitted", "assessment_id": 7, "outcome": "pending", **state}
    provider_data = {"owner": "flathub-infra", "repo": "vorarbeiter", "run_id": 99}
    return make_pipeline(provider_data={**provider_data, PROVIDER_DATA_KEY: state})


def assessment_server(outcome, requests=None, differences=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        body = {
            "assessment_id": 7,
            "outcome": outcome,
            "review_url": REVIEW_URL,
            "differences": differences,
        }
        return httpx.Response(200, json=body)

    return mock_http(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario, expected_status, reassess",
    [
        ("current", "pending", False),
        ("current_accepted", "success", False),
        ("base_changed", "pending", True),
        ("base_unknown", "pending", True),
        ("head_changed", None, False),
        ("retargeted", "pending", False),
        ("closed", None, False),
        ("merged", None, False),
        ("graphql_error", None, False),
        ("unavailable", None, False),
    ],
)
async def test_large_pr_refresh_preserves_assessment_checks(
    configured, local_db, db_session_maker, gh, scenario, expected_status, reassess
):
    pipeline = submitted(outcome="accepted")
    if scenario == "base_unknown":
        pipeline.params = {k: v for k, v in pipeline.params.items() if k != "base_sha"}
    await add(db_session_maker, pipeline)
    original = dict(pipeline.provider_data[PROVIDER_DATA_KEY])
    backend = Backend(
        outcome="accepted" if scenario == "current_accepted" else "pending"
    )
    metadata = {
        "state": {"closed": "CLOSED", "merged": "MERGED"}.get(scenario, "OPEN"),
        "headRefOid": NEW_BASE if scenario == "head_changed" else HEAD,
        "baseRefName": "beta" if scenario == "retargeted" else "master",
        "baseRefOid": NEW_BASE if scenario == "base_changed" else BASE,
    }
    github_requests = []

    def handler(request):
        if request.url.host != "api.github.com":
            return backend(request)
        github_requests.append(request)
        assert request.url.path == "/graphql"
        if scenario == "unavailable":
            return httpx.Response(403, json={"message": "Forbidden"})
        body = {"data": {"repository": {"pullRequest": metadata}}}
        if scenario == "graphql_error":
            # Even apparently complete partial data must not authorize a report.
            body["errors"] = [{"message": "Could not resolve pull request"}]
        return httpx.Response(200, json=body)

    with (
        mock_http(handler),
        patch.object(permission_assessment, "get_pull_request", get_pull_request),
        patch.object(
            permission_assessment,
            "get_build_job_arches",
            AsyncMock(return_value=["x86_64"]),
        ),
    ):
        if scenario in ("graphql_error", "unavailable"):
            with pytest.raises(
                RuntimeError, match="Failed to fetch permission pull request"
            ):
                await permission_assessment.refresh_permission_review(
                    "flathub/org.test.App", 42
                )
        else:
            await permission_assessment.refresh_permission_review(
                "flathub/org.test.App", 42
            )

    assert len(github_requests) == 1
    assert backend.posts == int(reassess)
    if scenario in ("current", "current_accepted"):
        assert [r.method for r in backend.requests] == ["GET"]
    if reassess:
        payload = json.loads(backend.requests[0].content)
        assert payload["base_revision"] == metadata["baseRefOid"]
        assert payload["pull_request_head_revision"] == HEAD
    if expected_status is None:
        gh.status.assert_not_awaited()
        gh.comment.assert_not_awaited()
    else:
        gh.status.assert_awaited_once()
        assert gh.status.await_args.args[:2] == (HEAD, expected_status)
    stored = (await load(db_session_maker, pipeline.id)).provider_data[
        PROVIDER_DATA_KEY
    ]
    if scenario in ("graphql_error", "unavailable", "head_changed"):
        assert stored == original
        assert not backend.requests
    if scenario in ("closed", "merged"):
        assert stored["closed"] is True
        assert not backend.requests


@pytest.mark.parametrize(
    "outcome, state, creates_comment",
    [
        ("accepted", "success", False),
        ("pending", "pending", True),
        ("rejected", "failure", True),
        ("error", "error", True),
    ],
)
@pytest.mark.asyncio
async def test_reports_outcome_once(
    configured, local_db, db_session_maker, gh, outcome, state, creates_comment
):
    await add(db_session_maker, submitted())

    with assessment_server(outcome):
        await submit_pending_assessments()
        await submit_pending_assessments()

    gh.status.assert_awaited_once()
    assert gh.status.await_args.args[:4] == (HEAD, state, "flathub/org.test.App", None)
    gh.comment.assert_awaited_once()
    assert REVIEW_URL not in gh.comment.await_args.args[3]
    assert gh.comment.await_args.args[4] is creates_comment


@pytest.mark.asyncio
async def test_pending_comment_shows_permission_diff(
    configured, local_db, db_session_maker, gh
):
    await add(db_session_maker, submitted())
    differences = [
        {
            "path": ["x86_64", "Context", "sockets"],
            "before": ["fallback-x11", "wayland"],
            "after": ["x11", "wayland"],
        },
        {
            "path": ["x86_64", "Session Bus Policy", "org.example.Service"],
            "before": None,
            "after": "talk",
        },
        {
            "path": ["x86_64", "Session Bus Policy", "org.freedesktop.Flatpak"],
            "before": "talk",
            "after": "own",
        },
    ]

    with assessment_server("pending", differences=differences):
        await submit_pending_assessments()

    body = gh.comment.await_args.args[3]
    assert (
        "```diff\n"
        "@@ x86_64 / Context / sockets @@\n"
        "- fallback-x11\n"
        "+ x11\n"
        "@@ x86_64 / Session Bus Policy @@\n"
        "+ org.example.Service=talk\n"
        "- org.freedesktop.Flatpak=talk\n"
        "+ org.freedesktop.Flatpak=own\n"
        "```\n\n"
        "Explain why these changes are needed; moderators may reject it."
    ) in body
    assert "Merging is blocked until a Flathub moderator approves." in body


@pytest.mark.asyncio
async def test_stale_head_reports_nothing(configured, local_db, db_session_maker, gh):
    await add(db_session_maker, submitted())
    gh.pr["head"]["sha"] = NEW_BASE

    with assessment_server("accepted"):
        await submit_pending_assessments()

    gh.status.assert_not_awaited()
    gh.comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_change_resubmits_with_new_base(
    configured, local_db, db_session_maker, gh
):
    pipeline = submitted()
    await add(db_session_maker, pipeline)
    gh.pr["base"]["sha"] = NEW_BASE
    requests = []

    with (
        assessment_server("accepted", requests),
        patch.object(
            permission_assessment,
            "get_build_job_arches",
            AsyncMock(return_value=["x86_64"]),
        ),
    ):
        await submit_pending_assessments()

    assert [r.method for r in requests] == ["POST"]
    assert json.loads(requests[0].content)["base_revision"] == NEW_BASE
    stored = await load(db_session_maker, pipeline.id)
    assert stored.provider_data[PROVIDER_DATA_KEY]["base_revision"] == NEW_BASE
    assert gh.status.await_args.args[1] == "success"


@pytest.mark.asyncio
async def test_closed_pull_request_stops_tracking(
    configured, local_db, db_session_maker, gh
):
    await add(db_session_maker, submitted())
    gh.pr["state"] = "closed"

    with assessment_server("accepted"):
        await submit_pending_assessments()
        await submit_pending_assessments()

    gh.get_pr.assert_awaited_once()
    gh.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_github_calls_without_status_channels(
    configured, local_db, db_session_maker, gh
):
    await add(db_session_maker, submitted())

    with (
        patch.object(settings, "permission_status_repos", []),
        assessment_server("accepted"),
    ):
        await submit_pending_assessments()

    gh.get_pr.assert_not_awaited()
    gh.status.assert_not_awaited()


class Backend:
    def __init__(self, **body):
        self.body = {"assessment_id": 7, "outcome": "pending", **body}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"review_url": REVIEW_URL, **self.body})

    @property
    def posts(self) -> int:
        return sum(request.method == "POST" for request in self.requests)


def notification_headers(secret=SECRET, **changes):
    now = datetime.now(UTC)
    claims = {
        "sub": "permission-assessment",
        "scope": "notify",
        "iat": now,
        "exp": now + timedelta(minutes=10),
        **changes,
    }
    claims = {key: value for key, value in claims.items() if value is not None}
    return {"Authorization": f"Bearer {jwt.encode(claims, secret, algorithm='HS256')}"}


async def notify(api, headers=None, **body):
    return await api.post(
        "/api/webhooks/permission-decisions",
        json={
            "source_repository": "flathub/org.test.App",
            "pull_request_number": 42,
            **body,
        },
        headers=notification_headers() if headers is None else headers,
    )


@pytest_asyncio.fixture
async def api():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.parametrize("pr_number", ["42", 42])
@pytest.mark.asyncio
async def test_notifications_report_current_decision(
    configured, local_db, db_session_maker, gh, api, pr_number
):
    pipeline = submitted()
    pipeline.params = {**pipeline.params, "pr_number": pr_number}
    await add(db_session_maker, pipeline)
    backend = Backend(
        outcome="rejected",
        decision_id=3,
        decision_rationale="Use the portal.\nThanks.",
        decision_reviewer="mod",
    )

    with mock_http(backend):
        assert (await notify(api)).status_code == 204
        assert (await notify(api)).status_code == 204
        gh.status.assert_awaited_once()
        assert gh.status.await_args.args[1] == "failure"
        body = gh.comment.await_args.args[3]
        assert "> Use the portal.\n> Thanks." in body
        assert "Merging is blocked" not in body
        assert "cc @flathub/reviewers" in body
        assert body.index("cc @flathub/reviewers") > body.index("> Thanks.")
        assert "@mod" not in body

        backend.body = {
            "outcome": "accepted",
            "decision_id": 4,
            "decision_reviewer": "mod",
        }
        assert (await notify(api)).status_code == 204
        assert gh.status.await_args.args[1] == "success"
        assert gh.comment.await_args.args[4] is True
        assert "@" not in gh.comment.await_args.args[3]

        backend.body = {
            "outcome": "accepted",
            "decision_id": 5,
            "decision_rationale": "Ok",
        }
        assert (await notify(api)).status_code == 204
        assert (await notify(api)).status_code == 204

    assert gh.status.await_count == 3
    assert gh.comment.await_count == 3
    assert "> Ok" in gh.comment.await_args.args[3]
    assert backend.posts == 0


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("accepted", "success"),
        ("rejected", "failure"),
    ],
)
@pytest.mark.asyncio
async def test_automatic_decision_rationale_visibility(
    configured, local_db, db_session_maker, gh, api, outcome, status
):
    await add(db_session_maker, submitted())
    backend = Backend(
        outcome=outcome,
        decision_id=3,
        decision_rationale="Rule rationale.",
        decision_automatic=True,
    )

    with mock_http(backend):
        assert (await notify(api)).status_code == 204

    assert gh.status.await_args.args[1] == status
    body = gh.comment.await_args.args[3]
    assert ("Rule rationale." in body) is (outcome == "rejected")


@pytest.mark.asyncio
async def test_notification_ignores_newer_unassessed_rebuild_and_obsolete_head(
    configured, local_db, db_session_maker, gh, api
):
    now = datetime.now(UTC)
    older = submitted()
    older.created_at = now - timedelta(minutes=5)
    rebuild = make_pipeline(status=PipelineStatus.FAILED, created_at=now)
    await add(db_session_maker, older, rebuild)

    with mock_http(Backend(outcome="accepted", decision_id=3)):
        gh.pr["head"]["sha"] = NEW_BASE
        assert (await notify(api)).status_code == 204
        gh.status.assert_not_awaited()
        gh.pr["head"]["sha"] = HEAD
        assert (await notify(api)).status_code == 204

    assert gh.status.await_args.args[1] == "success"


@pytest.mark.asyncio
async def test_late_rationale_updates_existing_report(
    configured, local_db, db_session_maker, gh, api
):
    await add(
        db_session_maker,
        submitted(
            outcome="rejected",
            review_url=REVIEW_URL,
            reported=["rejected", REVIEW_URL, None, ""],
        ),
    )

    backend = Backend(outcome="rejected", decision_id=3, decision_rationale="Too broad")
    with mock_http(backend):
        assert (await notify(api)).status_code == 204

    assert "> Too broad" in gh.comment.await_args.args[3]


@pytest.mark.asyncio
async def test_github_failure_returns_503_and_keeps_refreshed_state(
    configured, local_db, db_session_maker, gh, api
):
    pipeline = submitted()
    await add(db_session_maker, pipeline, make_pipeline(params={"pr_number": "7"}))
    gh.status.return_value = False

    with mock_http(Backend(outcome="rejected", decision_id=3)):
        assert (await notify(api)).status_code == 503
        state = (await load(db_session_maker, pipeline.id)).provider_data[
            PROVIDER_DATA_KEY
        ]
        assert (state["outcome"], state["decision_id"]) == ("rejected", 3)
        assert "reported" not in state
        gh.status.return_value = True
        assert (await notify(api)).status_code == 204
        assert (await notify(api, pull_request_number=7)).status_code == 204

    assert gh.status.await_count == 2


@pytest.mark.parametrize(
    "headers, status",
    [
        ({}, 401),
        (notification_headers(secret="wrong-secret-0123456789abcdef0123"), 401),
        (notification_headers(exp=datetime.now(UTC) - timedelta(minutes=1)), 401),
        (notification_headers(iat=None), 401),
        (notification_headers(sub="other"), 401),
        (notification_headers(scope="assess"), 403),
    ],
)
@pytest.mark.asyncio
async def test_notification_authentication(configured, gh, api, headers, status):
    assert (await notify(api, headers=headers)).status_code == status
    gh.get_pr.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"pull_request_number": "42"},
        {"pull_request_number": 0},
        {"source_repository": ""},
        {"outcome": "accepted"},
    ],
)
@pytest.mark.asyncio
async def test_notification_rejects_invalid_body(configured, api, body):
    assert (await notify(api, **body)).status_code == 422


@pytest.mark.asyncio
async def test_notification_without_configuration_is_unavailable(api):
    with patch.object(settings, "permission_assessment_secret", None):
        assert (await notify(api)).status_code == 503


@pytest.mark.asyncio
async def test_commit_submits_assessment_immediately(
    configured, local_db, db_session_maker, gh
):
    committed = make_pipeline(status=PipelineStatus.SUCCEEDED)
    running = make_pipeline(status=PipelineStatus.SUCCEEDED, commit_job_id=1)
    broken = make_pipeline(status=PipelineStatus.SUCCEEDED, commit_job_id=2)
    await add(db_session_maker, committed, running, broken)
    statuses = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            pipeline_id = uuid.UUID(json.loads(request.content)["pipeline_id"])
            statuses.append((await load(db_session_maker, pipeline_id)).status)
        return httpx.Response(
            200,
            json={"assessment_id": 7, "outcome": "pending", "review_url": REVIEW_URL},
        )

    jobs = {5678: 2, 1: 1, 2: 3}
    flat_manager = AsyncMock()
    flat_manager.get_job.side_effect = lambda job_id: {"status": jobs[job_id]}
    with (
        mock_http(handler),
        patch.object(
            permission_assessment,
            "get_build_job_arches",
            AsyncMock(return_value=["x86_64"]),
        ),
        patch(
            "app.services.job_monitor.get_flat_manager_client",
            return_value=flat_manager,
        ),
        patch.object(JobMonitor, "_notify_committed"),
        patch.object(JobMonitor, "_notify_flat_manager_job_completed"),
        patch.object(JobMonitor, "_create_job_failure_issue"),
        patch("app.services.github_notifier.GitHubNotifier"),
        patch("app.pipelines.build.BuildPipeline.start_pending_builds"),
    ):
        for _ in range(2):
            async with db_session_maker() as db:
                await JobMonitor(db=db).check_all_active_pipelines(db)

    assert statuses == [PipelineStatus.COMMITTED]
    state = (await load(db_session_maker, committed.id)).provider_data[
        PROVIDER_DATA_KEY
    ]
    assert (state["status"], state["assessment_id"], state["attempts"]) == (
        "submitted",
        7,
        1,
    )
    assert gh.status.await_args.args[1] == "pending"


@pytest.mark.asyncio
async def test_report_failure_keeps_immediate_submission(
    configured, local_db, db_session_maker, gh
):
    pipeline = make_pipeline()
    await add(db_session_maker, pipeline)
    gh.status.side_effect = RuntimeError("GitHub unavailable")
    backend = Backend()

    with (
        mock_http(backend),
        patch.object(
            permission_assessment,
            "get_build_job_arches",
            AsyncMock(return_value=["x86_64"]),
        ),
    ):
        await submit_pipeline_assessment(pipeline.id)
        state = (await load(db_session_maker, pipeline.id)).provider_data[
            PROVIDER_DATA_KEY
        ]
        assert (state["status"], state["attempts"]) == ("submitted", 1)
        assert "reported" not in state
        gh.status.side_effect = None
        await submit_pending_assessments()

    assert backend.posts == 1
    state = (await load(db_session_maker, pipeline.id)).provider_data[PROVIDER_DATA_KEY]
    assert state["reported"][0] == "pending"


@pytest.mark.asyncio
async def test_immediate_submission_respects_age_limit(
    configured, local_db, db_session_maker
):
    pipeline = make_pipeline(
        created_at=datetime.now(UTC) - permission_assessment.LOOKBACK - timedelta(1)
    )
    await add(db_session_maker, pipeline)
    backend = Backend()

    with mock_http(backend):
        await submit_pipeline_assessment(pipeline.id)
        assert await submit_pending_assessments() == {
            "status": "completed",
            "submitted": 0,
            "skipped": 0,
            "failed": 0,
        }

    assert backend.requests == []


@pytest.mark.asyncio
async def test_report_failures_keep_acquisition_results(
    configured, local_db, db_session_maker, gh, flat_manager
):
    skipped = make_pipeline()
    failing = make_pipeline(
        build_id=1235,
        params={"pr_number": "43"},
        provider_data={
            "owner": "flathub-infra",
            "repo": "vorarbeiter",
            "run_id": 99,
            PROVIDER_DATA_KEY: {"status": "failed", "attempts": MAX_ATTEMPTS - 1},
        },
    )
    await add(db_session_maker, skipped, failing)
    flat_manager.get_build_info.side_effect = lambda build_id: (
        build_info(refs=("runtime/org.test.App/x86_64/stable",))
        if build_id == skipped.build_id
        else build_info()
    )
    gh.status.side_effect = RuntimeError("GitHub unavailable")

    with (
        mock_http(lambda request: httpx.Response(502)),
        patch.object(
            permission_assessment,
            "get_build_job_arches",
            AsyncMock(return_value=["x86_64"]),
        ),
    ):
        result = await submit_pending_assessments()

    assert result == {"status": "completed", "submitted": 0, "skipped": 1, "failed": 1}
    assert gh.status.await_count == 2
    skipped_state = (await load(db_session_maker, skipped.id)).provider_data[
        PROVIDER_DATA_KEY
    ]
    failed_state = (await load(db_session_maker, failing.id)).provider_data[
        PROVIDER_DATA_KEY
    ]
    assert skipped_state["status"] == "skipped"
    assert (failed_state["status"], failed_state["attempts"]) == (
        "failed",
        MAX_ATTEMPTS,
    )


@pytest.mark.asyncio
async def test_base_reassessment_survives_report_failure(
    configured, local_db, db_session_maker, gh
):
    pipeline = submitted()
    await add(db_session_maker, pipeline)
    gh.pr["base"]["sha"] = NEW_BASE
    gh.status.return_value = False
    backend = Backend(assessment_id=8, outcome="accepted")

    with (
        mock_http(backend),
        patch.object(
            permission_assessment,
            "get_build_job_arches",
            AsyncMock(return_value=["x86_64"]),
        ),
    ):
        await submit_pending_assessments()
        state = (await load(db_session_maker, pipeline.id)).provider_data[
            PROVIDER_DATA_KEY
        ]
        assert (state["assessment_id"], state["base_revision"]) == (8, NEW_BASE)
        gh.status.return_value = True
        await submit_pending_assessments()

    assert [request.method for request in backend.requests] == ["POST", "GET"]
    assert gh.status.await_args.args[1] == "success"
