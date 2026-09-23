import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.models import Pipeline, PipelineStatus
from app.routes.dashboard import get_reproducibility_data


def make_pipeline(**overrides):
    now = datetime.now(UTC)
    defaults = {
        "id": uuid.uuid4(),
        "app_id": "org.test.App",
        "status": PipelineStatus.FAILED,
        "flat_manager_repo": "stable",
        "params": {"repo": "flathub/org.test.App", "sha": "abc123def456"},
        "created_at": now - timedelta(minutes=10),
        "started_at": now - timedelta(minutes=5),
        "finished_at": now,
        "log_url": None,
        "build_id": 123,
        "commit_job_id": None,
        "publish_job_id": None,
        "update_repo_job_id": None,
        "failure_issue_url": None,
    }
    defaults.update(overrides)
    return Pipeline(**defaults)


def test_builds_table_failed_badge_links_to_commit_job(client):
    pipeline = make_pipeline(commit_job_id=12345)

    with patch(
        "app.routes.dashboard.get_recent_pipelines",
        new=AsyncMock(return_value=[pipeline]),
    ):
        response = client.get("/api/htmx/builds")

    assert response.status_code == 200
    assert 'href="https://hub.flathub.org/status/12345"' in response.text
    assert ">failed</a>" in response.text


def test_builds_table_failed_badge_prefers_update_repo_job(client):
    pipeline = make_pipeline(
        commit_job_id=12345,
        publish_job_id=12346,
        update_repo_job_id=12347,
    )

    with patch(
        "app.routes.dashboard.get_recent_pipelines",
        new=AsyncMock(return_value=[pipeline]),
    ):
        response = client.get("/api/htmx/builds")

    assert response.status_code == 200
    assert 'href="https://hub.flathub.org/status/12347"' in response.text
    assert 'href="https://hub.flathub.org/status/12346"' not in response.text


def test_builds_table_failed_badge_prefers_failure_issue(client):
    pipeline = make_pipeline(
        commit_job_id=12345,
        failure_issue_url="https://github.com/flathub/org.test.App/issues/1",
    )

    with patch(
        "app.routes.dashboard.get_recent_pipelines",
        new=AsyncMock(return_value=[pipeline]),
    ):
        response = client.get("/api/htmx/builds")

    assert response.status_code == 200
    assert 'href="https://github.com/flathub/org.test.App/issues/1"' in response.text
    assert 'href="https://hub.flathub.org/status/12345"' not in response.text


def test_builds_table_failed_badge_falls_back_to_log_url(client):
    pipeline = make_pipeline(log_url="https://example.com/logs/123")

    with patch(
        "app.routes.dashboard.get_recent_pipelines",
        new=AsyncMock(return_value=[pipeline]),
    ):
        response = client.get("/api/htmx/builds")

    assert response.status_code == 200
    assert 'href="https://example.com/logs/123"' in response.text


def test_app_status_failed_badge_links_in_stable_table(client):
    stable_pipeline = make_pipeline(commit_job_id=12345)

    with (
        patch(
            "app.routes.dashboard.get_app_builds",
            new=AsyncMock(return_value=([stable_pipeline], {})),
        ),
        patch(
            "app.routes.dashboard.get_status_banner",
            new=AsyncMock(return_value=None),
        ),
    ):
        response = client.get("/status/org.test.App")

    assert response.status_code == 200
    assert 'href="https://hub.flathub.org/status/12345"' in response.text
    assert ">failed</a>" in response.text


def test_app_status_failed_badge_prefers_failure_issue(client):
    stable_pipeline = make_pipeline(
        commit_job_id=12345,
        failure_issue_url="https://github.com/flathub/org.test.App/issues/1",
    )

    with (
        patch(
            "app.routes.dashboard.get_app_builds",
            new=AsyncMock(return_value=([stable_pipeline], {})),
        ),
        patch(
            "app.routes.dashboard.get_status_banner",
            new=AsyncMock(return_value=None),
        ),
    ):
        response = client.get("/status/org.test.App")

    assert response.status_code == 200
    assert 'href="https://github.com/flathub/org.test.App/issues/1"' in response.text
    assert 'href="https://hub.flathub.org/status/12345"' not in response.text


@pytest.mark.asyncio
async def test_fleet_uses_latest_published_stable_build(db_session_maker):
    now = datetime.now(UTC)
    repro = make_pipeline(
        app_id="org.test.App",
        params={
            "workflow_id": "reprocheck.yml",
            "reprocheck_result": {"status_code": "0"},
        },
        status=PipelineStatus.SUCCEEDED,
    )
    old = make_pipeline(
        status=PipelineStatus.PUBLISHED,
        finished_at=now - timedelta(days=2),
        repro_pipeline_id=repro.id,
    )
    latest = make_pipeline(
        status=PipelineStatus.PUBLISHED,
        finished_at=now - timedelta(days=1),
        created_at=now - timedelta(days=1, hours=2),
        repro_pipeline_id=None,
        params={"sha": "new"},
    )
    tie_created_at = now - timedelta(days=1, hours=1)
    tied = [
        make_pipeline(
            id=uuid.UUID(int=(0xABCD << 112) + number),
            status=PipelineStatus.PUBLISHED,
            finished_at=latest.finished_at,
            created_at=tie_created_at,
            params={"sha": f"tie-{number}"},
        )
        for number in (1, 2)
    ]
    failed = make_pipeline(status=PipelineStatus.FAILED, finished_at=now)
    beta = make_pipeline(
        status=PipelineStatus.PUBLISHED,
        flat_manager_repo="beta",
        finished_at=now + timedelta(days=1),
    )
    null_finished = make_pipeline(
        status=PipelineStatus.PUBLISHED,
        finished_at=None,
        created_at=now,
        params={"sha": "null-finished"},
    )
    async with db_session_maker() as session:
        session.add_all([repro, old, latest, failed, beta, null_finished, *tied])
        await session.commit()

    @asynccontextmanager
    async def local_db(*, use_replica=False):
        async with db_session_maker() as session:
            yield session

    with patch("app.routes.dashboard.get_db", local_db):
        data = await get_reproducibility_data()
        assert len(data.unknown) == 1
        assert data.unknown[0].build_commit == "tie-2"
        assert data.reproducible == []


def test_reproducible_rejects_unknown_status(client):
    assert client.get("/api/reproducible?status=invalid").status_code == 422


def test_status_banner_json(client):
    banner = {
        "severity": "error",
        "label": "Outage",
        "summary_status": "down",
        "status_url": "https://status.example.org",
        "issues": [
            {
                "system": "Builds",
                "title": "Delayed",
                "permalink": "https://status.example.org/issue",
                "severity": "error",
            }
        ],
    }
    with patch(
        "app.routes.dashboard.get_status_banner", new=AsyncMock(return_value=banner)
    ):
        response = client.get("/api/status-banner")
    assert response.status_code == 200
    assert response.json()["issues"][0]["system"] == "Builds"
