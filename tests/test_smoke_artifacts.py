import base64
import io
import json
import zipfile
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest

from app.services.smoke_artifacts import download_artifact, parse_result_archive
from app.smoke import collect_results

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


def collected_report(tmp_path, failure=None, execution=None, missing=False):
    source, results, output = (
        tmp_path / name for name in ("input", "results", "report")
    )
    source.mkdir()
    (source / "request.json").write_text(
        json.dumps(
            {
                "state": "ready",
                "recipe": "source/recipe.yml",
                "sha": "a" * 40,
                "app_ref": "app/org.example.App/x86_64/test",
            }
        )
    )
    for mode in ("verify", "screenshots"):
        root = results / mode
        root.mkdir(parents=True)
        if missing and mode == "screenshots":
            continue
        (root / "result.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "failed"
                    if failure and mode == "screenshots"
                    else "passed",
                    "failure": failure if mode == "screenshots" else None,
                    "screenshots": ["screenshots/000-overview.png"],
                    "phase_metadata": {"app": None, "runtimes": None, "recipe": None},
                }
            )
        )
        (root / "screenshots").mkdir()
        (root / "screenshots/000-overview.png").write_bytes(PNG)
    if not missing:
        (results / "screenshots/screenshots.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "captures": [
                        {"path": "screenshots/000-overview.png", "caption": "Overview"}
                    ],
                }
            )
        )
    if execution:
        (results / "screenshots.execution.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "exit_code": 0,
                    "cleanup": {},
                    **execution,
                }
            )
        )
    collect_results(
        source,
        results,
        output,
        {
            "verify": "success",
            "screenshots": "failure" if failure or missing else "success",
        },
    )
    return parse_result_archive(
        archive(
            {
                p.relative_to(output).as_posix(): p.read_bytes()
                for p in output.rglob("*")
                if p.is_file()
            }
        )
    )


@pytest.mark.parametrize(
    "reason,category,expected",
    [
        ("screenshot_failed", "infrastructure", "infrastructure_error"),
        ("screenshot_failed", "expectation", "failed"),
        ("screenshot_failed", None, "infrastructure_error"),
        ("screenshot_failed", "future_category", "infrastructure_error"),
        ("window_timeout", "expectation", "failed"),
        ("early_exit", None, "failed"),
        ("display_exited", None, "infrastructure_error"),
        ("install_failed", None, "infrastructure_error"),
    ],
)
def test_v051_failure_categories_survive_collection(
    tmp_path, reason, category, expected
):
    result = collected_report(
        tmp_path, {"reason": reason, "category": category, "message": "Diagnostic"}
    )
    assert result.summary["verify"] == "passed"
    assert result.summary["screenshots"] == expected
    assert result.summary["previews"][0]["caption"] == "Overview"
    assert len(result.images) == 2


@pytest.mark.parametrize(
    "execution,missing,expected",
    [
        ({"status": "completed"}, False, "passed"),
        ({"status": "completed", "exit_code": 1}, False, "infrastructure_error"),
        ({"status": "timeout"}, True, "infrastructure_error"),
        ({"status": "docker_start_failed"}, True, "infrastructure_error"),
        ({"status": "cancelled"}, True, "cancelled"),
        ({"status": "running"}, False, "infrastructure_error"),
        (
            {"status": "completed", "cleanup": {"remove": {"status": "timeout"}}},
            False,
            "infrastructure_error",
        ),
    ],
)
def test_wrapper_reports_survive_collection(tmp_path, execution, missing, expected):
    result = collected_report(tmp_path, execution=execution, missing=missing)
    assert result.summary["verify"] == "passed"
    assert result.summary["screenshots"] == expected


def test_wrapper_timeout_preserves_specific_cli_failure(tmp_path):
    result = collected_report(
        tmp_path,
        {"reason": "window_timeout", "message": "No editor window"},
        {"status": "timeout"},
    )
    assert result.summary["screenshots"] == "failed"
    assert "screenshots: No editor window" in result.summary["messages"]
    assert "screenshots: wrapper timeout" in result.summary["messages"]


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
