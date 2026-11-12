import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import pytest

from app.config import settings
from app.main import app
from app.models import Pipeline, PipelineStatus, SmokeResult
from app.services.smoke_artifacts import SmokeArtifact, _cache
from app.smoke import collect_results
from tests.test_smoke_artifacts import PNG, archive, run_result


@pytest.mark.asyncio
async def test_preview_route_requires_recorded_artifact_and_image(db_session_maker):
    pipeline_id = uuid.uuid4()
    path = "verify/screenshots/000.png"
    async with db_session_maker() as db:
        db.add(
            Pipeline(
                id=pipeline_id,
                app_id="org.example.App",
                params={},
                status=PipelineStatus.SUCCEEDED,
            )
        )
        await db.flush()
        db.add(
            SmokeResult(
                pipeline_id=pipeline_id,
                run_id=20,
                run_attempt=1,
                source_attempt=1,
                artifact_id=40,
                summary={"previews": [{"path": path}]},
                notified=True,
            )
        )
        await db.commit()
    github = AsyncMock()
    github.request.return_value = httpx.Response(
        200, json={"id": 40, "workflow_run": {"id": 20}}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with (
            patch("app.routes.smoke.get_github_actions_client", return_value=github),
            patch(
                "app.routes.smoke.download_artifact",
                return_value=SmokeArtifact({}, {path: PNG}),
            ) as download,
        ):
            response = await client.get(f"/smoke/{pipeline_id}/40/{path}")
            assert response.status_code == 200
            assert response.content == PNG
            assert response.headers["content-type"] == "image/png"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert (
                await client.get(f"/smoke/{pipeline_id}/41/{path}")
            ).status_code == 404
            assert (
                await client.get(f"/smoke/{pipeline_id}/40/report.json")
            ).status_code == 404
            assert download.call_count == 1
        with (
            patch("app.routes.smoke.get_github_actions_client", return_value=github),
            patch("app.routes.smoke.download_artifact", side_effect=FileNotFoundError),
        ):
            assert (
                await client.get(f"/smoke/{pipeline_id}/40/{path}")
            ).status_code == 410


def test_workflow_webhook_requires_signature_and_ignores_other_repositories(client):
    def send(repo, signed):
        payload = json.dumps(
            {
                "action": "completed",
                "repository": {"full_name": repo},
                "sender": {"login": "bot"},
                "workflow_run": {"id": 20},
            }
        ).encode()
        headers = {
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-GitHub-Event": "workflow_run",
        }
        if signed:
            digest = hmac.new(
                settings.github_webhook_secret.encode(), payload, hashlib.sha256
            ).hexdigest()
            headers["X-Hub-Signature-256"] = "sha256=" + digest
        return client.post("/api/webhooks/github", content=payload, headers=headers)

    with patch(
        "app.services.smoke.SmokeService.process_run", return_value=True
    ) as process:
        assert send("flathub-infra/vorarbeiter", False).status_code == 401
        assert send("someone/else", True).status_code == 202
        process.assert_not_called()
        assert send("flathub-infra/vorarbeiter", True).status_code == 202
        process.assert_awaited_once_with(20)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["passed", "failed", "missing", "timeout"])
async def test_collected_results_reach_signed_webhook_comment_and_preview(
    db_session_maker, monkeypatch, tmp_path, outcome
):
    source, results, report = (
        tmp_path / name for name in ("input", "results", "report")
    )
    source.mkdir()
    (source / "request.json").write_text(
        json.dumps(
            {
                "state": "ready",
                "sha": "a" * 40,
                "app_ref": "app/org.example.App/x86_64/test",
                "recipe": "",
            }
        )
    )
    if outcome in {"passed", "failed"}:
        (results / "verify/screenshots").mkdir(parents=True)
        (results / "verify/screenshots/000.png").write_bytes(PNG)
        (results / "verify/result.json").write_text(
            json.dumps(
                {
                    **run_result(),
                    "status": outcome,
                    "screenshots": ["screenshots/000.png"],
                    "failure": {"reason": "early_exit", "message": "App exited"}
                    if outcome == "failed"
                    else None,
                }
            )
        )
    if outcome == "timeout":
        results.mkdir()
        (results / "verify.execution.json").write_text(
            json.dumps({"schema_version": 1, "status": "timeout", "cleanup": {}})
        )
    collect_results(
        source,
        results,
        report,
        {
            "verify": "success" if outcome == "passed" else "failure",
            "screenshots": "skipped",
        },
    )
    payload = archive(
        {
            p.relative_to(report).as_posix(): p.read_bytes()
            for p in report.rglob("*")
            if p.is_file()
        }
    )
    pipeline_id = uuid.uuid4()
    async with db_session_maker() as db:
        db.add(
            Pipeline(
                id=pipeline_id,
                app_id="org.example.App",
                status=PipelineStatus.COMMITTED,
                params={
                    "repo": "flathub/org.example.App",
                    "pr_number": 12,
                    "sha": "a" * 40,
                },
                log_url="https://github.com/flathub-infra/vorarbeiter/actions/runs/10",
            )
        )
        await db.commit()
    artifact = {
        "id": 40,
        "name": "smoke-result-1",
        "expired": False,
        "size_in_bytes": len(payload),
        "workflow_run": {"id": 20},
    }
    writes = []

    def upstream(request):
        path = request.url.path
        if request.url.host == "test.blob.core.windows.net":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=payload)
        assert request.url.host == "api.github.com"
        if path.endswith("/actions/runs/20"):
            data = {
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "success",
                "path": ".github/workflows/smoke.yml",
                "event": "workflow_run",
                "display_title": "smoke build 10 attempt 1",
            }
        elif path.endswith("/actions/runs/10/attempts/1"):
            data = {
                "path": ".github/workflows/build.yml",
                "event": "workflow_dispatch",
                "conclusion": "success",
            }
        elif path.endswith("/actions/runs/10/artifacts"):
            data = {"artifacts": [{"id": 30, "name": "smoke-input-1-x86_64"}]}
        elif path.endswith("/actions/runs/20/artifacts"):
            data = {"artifacts": [artifact]}
        elif path.endswith("/actions/artifacts/40/zip"):
            return httpx.Response(
                302,
                headers={"Location": "https://test.blob.core.windows.net/report.zip"},
            )
        elif path.endswith("/actions/artifacts/40"):
            data = artifact
        elif path.endswith("/pulls/12"):
            data = {"state": "open", "head": {"sha": "a" * 40}}
        elif path == "/user":
            data = {"id": 123}
        elif path.endswith("/issues/12/comments"):
            if request.method == "POST":
                writes.append(json.loads(request.content)["body"])
                data = {"id": 99}
            else:
                data = []
        else:
            raise AssertionError((request.method, path))
        return httpx.Response(200, json=data)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(upstream), **kw),
    )
    _cache.clear()
    event = json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "flathub-infra/vorarbeiter"},
            "sender": {"login": "bot"},
            "workflow_run": {"id": 20},
        }
    ).encode()
    headers = {
        "X-GitHub-Event": "workflow_run",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "X-Hub-Signature-256": "sha256="
        + hmac.new(
            settings.github_webhook_secret.encode(), event, hashlib.sha256
        ).hexdigest(),
    }
    async with original(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/api/webhooks/github", content=event, headers=headers
        )
        assert first.status_code == 202
        assert first.json()["smoke_reported"] is True
        repeat = await client.post(
            "/api/webhooks/github", content=event, headers=headers
        )
        assert repeat.status_code == 202
        assert repeat.json()["smoke_reported"] is False
        assert len(writes) == 1
        verdict = outcome if outcome in {"passed", "failed"} else "infrastructure error"
        assert f"Startup verification: **{verdict}**" in writes[0]
        assert "These checks do not block publication" in writes[0]
        assert "actions/runs/20/artifacts/40" in writes[0]
        if outcome in {"passed", "failed"}:
            image_path = f"/smoke/{pipeline_id}/40/verify/screenshots/000.png"
            assert image_path in writes[0]
            preview = await client.get(image_path)
            assert preview.status_code == 200
            assert preview.content == PNG
        else:
            assert "![" not in writes[0]
        if outcome == "timeout":
            assert "wrapper timeout" in writes[0]
    async with db_session_maker() as db:
        assert (await db.get(Pipeline, pipeline_id)).status == PipelineStatus.COMMITTED
        saved = await db.get(SmokeResult, pipeline_id)
        assert saved.notified and saved.artifact_id == 40
    _cache.clear()
