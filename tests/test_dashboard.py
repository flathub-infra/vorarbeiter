import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.models import Pipeline, PipelineStatus


def make_pipeline(**overrides):
    now = datetime.now()
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
