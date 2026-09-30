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
