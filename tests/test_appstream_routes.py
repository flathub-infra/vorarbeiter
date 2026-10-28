import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from markupsafe import Markup

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.services.appstream import PreviewError, PreviewUnavailable
from tests.conftest import create_mock_get_db


@pytest.fixture
def preview_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        build_id=123,
        status=PipelineStatus.COMMITTED,
        flat_manager_repo="test",
        params={"repo": "flathub/flathub", "pr_number": "42", "sha": "abc123"},
        log_url="https://github.com/example/actions/runs/123",
        triggered_by=PipelineTrigger.WEBHOOK,
        created_at=datetime.now(UTC),
    )


def test_preview_renders_build_identity_and_safe_metadata(
    client, mock_db, preview_pipeline
):
    mock_db.get.return_value = preview_pipeline
    context = {
        "arch": "aarch64",
        "name": "<script>alert(1)</script>",
        "summary": "A useful app",
        "developer": None,
        "app_id": "org.test.App",
        "license": "MIT",
        "category": "Utility",
        "homepage": None,
        "homepage_host": None,
        "description": Markup("<p>Safe <em>markup</em>.</p>"),
        "gallery_style": "",
        "shots": [],
        "release": None,
        "links": [],
        "icon_url": "https://dl.flathub.org/build-repo/123/appstream/aarch64/icons/128x128/org.test.App.png",
    }
    with (
        patch("app.routes.appstream.get_db", create_mock_get_db(mock_db)),
        patch("app.routes.appstream.fetch_preview", AsyncMock(return_value=context)),
    ):
        response = client.get(f"/appstream/{preview_pipeline.id}")
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "<p>Safe <em>markup</em>.</p>" in response.text
    assert "https://github.com/flathub/flathub/pull/42" in response.text
    assert "aarch64" in response.text and "abc123" in response.text
    assert f'src="{context["icon_url"]}"' in response.text
    assert "script-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


@pytest.mark.parametrize(
    ("state", "build_id", "error", "status"),
    [
        (None, 123, None, 404),
        (PipelineStatus.COMMITTED, None, None, 404),
        (PipelineStatus.SUCCEEDED, 123, None, 409),
        (PipelineStatus.FAILED, 123, None, 404),
        (PipelineStatus.COMMITTED, 123, PreviewUnavailable(), 404),
        (PipelineStatus.COMMITTED, 123, PreviewError(), 502),
    ],
)
def test_preview_unavailable(
    client, mock_db, preview_pipeline, state, build_id, error, status
):
    preview_pipeline.status = state
    preview_pipeline.build_id = build_id
    mock_db.get.return_value = preview_pipeline if state else None
    fetch = AsyncMock(side_effect=error)
    with (
        patch("app.routes.appstream.get_db", create_mock_get_db(mock_db)),
        patch("app.routes.appstream.fetch_preview", fetch),
    ):
        response = client.get(f"/appstream/{preview_pipeline.id}")
    assert response.status_code == status
    assert response.headers["content-type"].startswith("text/html")
    if state != PipelineStatus.COMMITTED or not build_id:
        fetch.assert_not_called()
