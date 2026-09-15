from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.models import InactiveRepoSnapshot
from app.routes.inactive_repos import load_override_file
from tests.conftest import create_mock_get_db


def snapshot(candidates: list[str]):
    timestamp = datetime(2026, 9, 15, tzinfo=UTC)
    return InactiveRepoSnapshot(
        organization="flathub",
        scan_started_at=timestamp,
        scan_completed_at=timestamp,
        automatic_candidates=candidates,
    )


def write_overrides(directory: Path, exclude: str = "", manual: str = ""):
    directory.mkdir(exist_ok=True)
    (directory / "exclude.txt").write_text(exclude, encoding="utf-8")
    (directory / "manual_inactive.txt").write_text(manual, encoding="utf-8")


def test_missing_snapshot_returns_503(client, mock_db, tmp_path):
    mock_db.get = AsyncMock(return_value=None)
    write_overrides(tmp_path)

    with (
        patch("app.routes.inactive_repos.get_db", create_mock_get_db(mock_db)),
        patch("app.routes.inactive_repos.OVERRIDE_DIRECTORY", tmp_path),
    ):
        response = client.get("/api/inactive-repos.txt")

    assert response.status_code == 503


def test_initialized_empty_snapshot_returns_empty_200(client, mock_db, tmp_path):
    mock_db.get = AsyncMock(return_value=snapshot([]))
    write_overrides(tmp_path)

    with (
        patch("app.routes.inactive_repos.get_db", create_mock_get_db(mock_db)),
        patch("app.routes.inactive_repos.OVERRIDE_DIRECTORY", tmp_path),
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


def test_endpoint_applies_override_precedence_and_sorting(client, mock_db, tmp_path):
    mock_db.get = AsyncMock(return_value=snapshot(["z", "automatic", "both"]))
    write_overrides(
        tmp_path,
        exclude=" automatic\n both\nautomatic\n",
        manual="# keep\nboth\nmanual\nmanual\n",
    )

    with (
        patch("app.routes.inactive_repos.get_db", create_mock_get_db(mock_db)),
        patch("app.routes.inactive_repos.OVERRIDE_DIRECTORY", tmp_path),
    ):
        response = client.get("/api/inactive-repos.txt")

    assert response.status_code == 200
    assert response.text == "both\nmanual\nz\n"


@pytest.mark.parametrize(
    "entry",
    [".", "..", "owner/repository", "bad name", "é", "control\x00name"],
)
def test_override_parser_rejects_malformed_names(tmp_path, entry):
    path = tmp_path / "override.txt"
    path.write_text(f"{entry}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Malformed repository basename"):
        load_override_file(path)


def test_malformed_override_returns_500(client, mock_db, tmp_path):
    mock_db.get = AsyncMock(return_value=snapshot(["automatic"]))
    write_overrides(tmp_path, manual="owner/repository\n")

    with (
        patch("app.routes.inactive_repos.get_db", create_mock_get_db(mock_db)),
        patch("app.routes.inactive_repos.OVERRIDE_DIRECTORY", tmp_path),
    ):
        response = client.get("/api/inactive-repos.txt")

    assert response.status_code == 500
    assert response.json() == {"detail": "Inactive repository overrides unavailable"}
