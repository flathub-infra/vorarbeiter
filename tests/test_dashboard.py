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


def test_dashboard_redirect(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "https://flathub.org/builds"


def test_dashboard_redirect_preserves_recognized_filters(client):
    response = client.get(
        "/?app_id=org.test.App&target=stable&status=failed"
        "&date_from=2026-09-23T00%3A00&date_to=2026-09-23T23%3A59"
        "&next=https%3A%2F%2Fevil.example",
        follow_redirects=False,
    )
    assert response.status_code == 308
    assert response.headers["location"] == (
        "https://flathub.org/builds?appId=org.test.App&repo=stable"
        "&status=failed&dateFrom=2026-09-23T00%3A00&dateTo=2026-09-23T23%3A59"
    )


def test_app_status_redirect_encodes_app_id(client):
    response = client.get("/status/org.test.App", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == (
        "https://flathub.org/builds/apps/org.test.App"
    )

    response = client.get("/status/org.test%20App", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == (
        "https://flathub.org/builds/apps/org.test%20App"
    )


def test_reproducible_redirect_drops_unknown_filters(client):
    response = client.get(
        "/reproducible?app_id=org.test.App&status=failed"
        "&next=https%3A%2F%2Fevil.example",
        follow_redirects=False,
    )
    assert response.status_code == 308
    assert response.headers["location"] == (
        "https://flathub.org/builds/reproducible?appId=org.test.App&status=failed"
    )


@pytest.mark.parametrize("path", ["/api/htmx/builds", "/api/htmx/reproducible"])
def test_removed_htmx_routes_return_404(client, path):
    assert client.get(path, follow_redirects=False).status_code == 404


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
