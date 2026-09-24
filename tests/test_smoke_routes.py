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
from app.services.smoke_artifacts import SmokeArtifact
from tests.test_smoke_artifacts import PNG


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
