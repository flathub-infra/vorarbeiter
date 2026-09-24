import base64
import io
import json
import zipfile
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest

from app.services.smoke_artifacts import download_artifact, parse_result_archive

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for name, content in files.items():
            z.writestr(
                name, content if isinstance(content, bytes) else json.dumps(content)
            )
    return data.getvalue()


def test_report_keeps_modes_independent_and_preserves_failure_preview():
    result = parse_result_archive(
        archive(
            {
                "report.json": {
                    "schema_version": 1,
                    "request": {
                        "state": "ready",
                        "recipe": "source/recipe.yml",
                        "sha": "a" * 40,
                        "app_ref": "app/org.example.App/x86_64/test",
                    },
                    "outcomes": {"verify": "success", "screenshots": "failure"},
                },
                "verify/result.json": {
                    "status": "passed",
                    "screenshots": [],
                    "failure": None,
                },
                "screenshots/result.json": {
                    "status": "failed",
                    "screenshots": ["logs/last-candidate.png"],
                    "failure": {
                        "reason": "window_timeout",
                        "message": "No matching window",
                    },
                },
                "screenshots/logs/last-candidate.png": PNG,
            }
        )
    )
    assert result.summary["verify"] == "passed"
    assert result.summary["screenshots"] == "failed"
    assert result.summary["previews"] == [
        {
            "path": "screenshots/logs/last-candidate.png",
            "caption": "Screenshot diagnostic",
        }
    ]
    assert result.images["screenshots/logs/last-candidate.png"] == PNG


@pytest.mark.parametrize(
    "name", ["../outside.png", "/outside.png", "verify/../outside.png"]
)
def test_archive_rejects_traversal(name):
    with pytest.raises(ValueError):
        parse_result_archive(archive({name: PNG}))


def test_archive_rejects_duplicate_names():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("report.json", "{}")
        with pytest.warns(UserWarning):
            z.writestr("report.json", "{}")
    with pytest.raises(ValueError, match="duplicate"):
        parse_result_archive(data.getvalue())


def test_json_member_limit_is_checked_before_parsing(monkeypatch):
    monkeypatch.setattr("app.services.smoke_artifacts.MAX_JSON", 4)
    with pytest.raises(ValueError, match="oversized"):
        parse_result_archive(archive({"report.json": b" " * 5}))


def test_missing_mode_result_is_infrastructure_error():
    result = parse_result_archive(
        archive(
            {
                "report.json": {
                    "schema_version": 1,
                    "request": {
                        "state": "ready",
                        "recipe": "",
                        "sha": "a" * 40,
                        "app_ref": "app/org.example.App/x86_64/test",
                    },
                    "outcomes": {"verify": "failure"},
                }
            }
        )
    )
    assert result.summary["verify"] == "infrastructure_error"
    assert result.summary["screenshots"] == "skipped"


@pytest.mark.asyncio
async def test_download_does_not_send_credentials_to_storage(monkeypatch):
    from unittest.mock import AsyncMock

    import httpx2 as httpx

    from app.services.smoke_artifacts import _cache, download_artifact

    _cache.clear()
    github = AsyncMock()
    github.request.return_value = httpx.Response(
        302, headers={"Location": "https://test.blob.core.windows.net/report.zip"}
    )
    monkeypatch.setattr(
        "app.services.smoke_artifacts.get_github_actions_client", lambda: github
    )
    payload = archive(
        {"report.json": {"schema_version": 1, "request": {"state": "skipped"}}}
    )
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(200, content=payload)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.smoke_artifacts.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(transport), **kw),
    )
    await download_artifact({"id": 1, "size_in_bytes": len(payload), "expired": False})
    assert "authorization" not in requests[0].headers
    assert github.request.call_args.kwargs["follow_redirects"] is False


@pytest.mark.asyncio
async def test_download_rejects_non_storage_redirect(monkeypatch):
    from unittest.mock import AsyncMock

    import httpx2 as httpx

    from app.services.smoke_artifacts import download_artifact

    github = AsyncMock()
    github.request.return_value = httpx.Response(
        302, headers={"Location": "http://localhost/secrets"}
    )
    monkeypatch.setattr(
        "app.services.smoke_artifacts.get_github_actions_client", lambda: github
    )
    with pytest.raises(ValueError, match="download host"):
        await download_artifact({"id": 2, "size_in_bytes": 1, "expired": False})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, 429, 503])
async def test_transient_download_errors_remain_retryable(monkeypatch, status):
    github = AsyncMock()
    github.request.return_value = None if status is None else httpx.Response(status)
    monkeypatch.setattr(
        "app.services.smoke_artifacts.get_github_actions_client", lambda: github
    )
    with pytest.raises(RuntimeError):
        await download_artifact({"id": 999, "size_in_bytes": 1, "expired": False})
