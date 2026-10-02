import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import jwt
import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Pipeline, PipelineStatus
from app.services import permission_assessment
from app.services.permission_assessment import (
    MAX_ATTEMPTS,
    PROVIDER_DATA_KEY,
    build_assessment_request,
    build_push_assessment_request,
    submit_pending_assessments,
)

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


@pytest.mark.asyncio
async def test_submits_published_push_build_with_linked_pull_request(
    configured, local_db, db_session_maker
):
    published = make_push_pipeline(status=PipelineStatus.PUBLISHED)
    direct = make_push_pipeline(status=PipelineStatus.COMMITTED)
    direct.params = {**direct.params, "sha": "1" * 40}
    running = make_push_pipeline(status=PipelineStatus.RUNNING)
    await add(db_session_maker, published, direct, running)
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
    assert set(bodies) == {str(published.id), str(direct.id)}
    linked_body = bodies[str(published.id)]
    assert linked_body["candidate_kind"] == "push"
    assert linked_body["pull_request_number"] == 7
    assert linked_body["pull_request_head_revision"] == PR_HEAD
    assert linked_body["built_revision"] == PUSHED
    assert "pull_request_number" not in bodies[str(direct.id)]

    stored = await load(db_session_maker, published.id)
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
