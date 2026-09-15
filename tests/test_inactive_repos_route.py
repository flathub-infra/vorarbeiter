from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from app.models import InactiveRepoSnapshot
from tests.conftest import create_mock_get_db


def snapshot(candidates: list[str]):
    timestamp = datetime(2026, 9, 15, tzinfo=UTC)
    return InactiveRepoSnapshot(
        organization="flathub",
        scan_started_at=timestamp,
        scan_completed_at=timestamp,
        automatic_candidates=candidates,
    )


def test_missing_snapshot_returns_503(client, mock_db):
    mock_db.get = AsyncMock(return_value=None)

    with (
        patch("app.routes.inactive_repos.get_db", create_mock_get_db(mock_db)),
    ):
        response = client.get("/api/inactive-repos.txt")

    assert response.status_code == 503


def test_initialized_empty_snapshot_returns_empty_200(client, mock_db):
    mock_db.get = AsyncMock(return_value=snapshot([]))

    with (
        patch("app.routes.inactive_repos.get_db", create_mock_get_db(mock_db)),
    ):
        response = client.get("/api/inactive-repos.txt")

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert (
        response.headers["x-inactive-repos-snapshot-completed-at"]
        == "2026-09-15T00:00:00+00:00"
    )


def test_endpoint_returns_sorted_candidates(client, mock_db):
    mock_db.get = AsyncMock(return_value=snapshot(["z", "automatic", "both"]))

    with (
        patch("app.routes.inactive_repos.get_db", create_mock_get_db(mock_db)),
    ):
        response = client.get("/api/inactive-repos.txt")

    assert response.status_code == 200
    assert response.text == "automatic\nboth\nz\n"
