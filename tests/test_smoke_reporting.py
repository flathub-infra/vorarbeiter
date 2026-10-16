import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import pytest
import pytest_asyncio

from app.models import Pipeline, PipelineStatus
from app.services.smoke import SmokeService
from app.services.smoke_artifacts import API, SmokeArtifact


@pytest_asyncio.fixture
async def reporting(db_session_maker):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.example.App",
        status=PipelineStatus.PUBLISHED,
        params={"repo": "flathub/org.example.App", "pr_number": "12", "sha": "a" * 40},
        log_url="https://github.com/flathub-infra/vorarbeiter/actions/runs/10",
    )
    async with db_session_maker() as db:
        db.add(pipeline)
        await db.commit()
    state: dict[str, Any] = {
        "head": "a" * 40,
        "comment": None,
        "writes": [],
        "fail_write": False,
        "attempt": 1,
        "path": ".github/workflows/smoke.yml",
    }

    async def request(method, url, **kwargs):
        if url == f"{API}/actions/runs/20":
            data = {
                "id": 20,
                "run_attempt": state["attempt"],
                "status": "completed",
                "conclusion": "success",
                "path": state["path"],
                "event": "workflow_run",
                "display_title": "smoke build 10 attempt 1",
            }
        elif url == f"{API}/actions/runs/10/attempts/1":
            data = {
                "path": ".github/workflows/build.yml",
                "event": "workflow_dispatch",
                "conclusion": "success",
            }
        elif url.endswith("/actions/runs/10/artifacts"):
            data = {"artifacts": [{"id": 30, "name": "smoke-input-1-x86_64"}]}
        elif url.endswith("/actions/runs/20/artifacts"):
            data = {
                "artifacts": [
                    {
                        "id": 40,
                        "name": f"smoke-result-{state['attempt']}",
                        "expired": False,
                    }
                ]
            }
        elif url.endswith("/actions/workflows/smoke.yml/runs"):
            assert "created" not in kwargs["params"]
            data = {"workflow_runs": [{"id": 20, "run_attempt": state["attempt"]}]}
        elif url.endswith("/pulls/12"):
            data = {"state": "open", "head": {"sha": state["head"]}}
        elif url.endswith("/user"):
            data = {"id": 123}
        elif url.endswith(("/issues/12/comments", "/issues/comments/99")):
            if method in {"post", "patch"}:
                if method == "post":
                    assert kwargs["max_retries"] == 0
                if state["fail_write"]:
                    return None
                body = json.loads(kwargs["content"])["body"]
                state["writes"].append((method, body))
                state["comment"] = {"id": 99, "user": {"id": 123}, "body": body}
                data = {"id": 99}
            else:
                data = [state["comment"]] if state["comment"] else []
        else:
            raise AssertionError((method, url, kwargs))
        return httpx.Response(200, json=data)

    github = AsyncMock()
    github.request.side_effect = request
    artifact = SmokeArtifact(
        {
            "verify": "failed",
            "screenshots": "skipped",
            "messages": ["No window <script> @someone"],
            "previews": [],
            "arch": "x86_64",
        },
        {},
        "a" * 40,
        "app/org.example.App/x86_64/test",
    )
    state["artifact"] = artifact
    with (
        patch("app.services.smoke.get_github_actions_client", return_value=github),
        patch("app.services.smoke.get_github_client", return_value=github),
        patch(
            "app.services.smoke_artifacts.get_github_actions_client",
            return_value=github,
        ),
        patch("app.services.smoke.download_artifact", return_value=artifact),
    ):
        yield state
    async with db_session_maker() as db:
        saved = await db.get(Pipeline, pipeline.id)
        assert saved.status == PipelineStatus.PUBLISHED
        assert saved.params == pipeline.params


@pytest.mark.asyncio
async def test_failed_checks_comment_once_without_changing_publication(reporting):
    await SmokeService().process_run(20)
    await SmokeService().process_run(20)
    assert len(reporting["writes"]) == 1
    method, body = reporting["writes"][0]
    assert method == "post"
    assert "Application startup and screenshot checks" in body and "failed" in body
    assert "These checks do not block publication" in body
    assert "<script>" not in body and "@someone" not in body


@pytest.mark.asyncio
async def test_rerun_updates_comment_instead_of_posting_again(reporting):
    await SmokeService().process_run(20)
    reporting["attempt"] = 2
    await SmokeService().process_run(20)
    assert [method for method, _ in reporting["writes"]] == ["post", "patch"]


@pytest.mark.asyncio
async def test_old_commit_does_not_post(reporting):
    reporting["head"] = "b" * 40
    await SmokeService().process_run(20)
    assert reporting["writes"] == []


@pytest.mark.asyncio
async def test_old_build_cannot_overwrite_newer_comment(reporting):
    reporting["comment"] = {
        "id": 99,
        "user": {"id": 123},
        "body": "<!-- vorarbeiter-smoke:11:1:21:1 -->",
    }
    await SmokeService().process_run(20)
    assert reporting["writes"] == []


@pytest.mark.asyncio
async def test_failed_comment_can_be_retried(reporting):
    reporting["fail_write"] = True
    with pytest.raises(RuntimeError):
        await SmokeService().process_run(20)
    reporting["fail_write"] = False
    await SmokeService().process_run(20)
    assert len(reporting["writes"]) == 1


@pytest.mark.asyncio
async def test_unrelated_workflow_is_ignored(reporting):
    reporting["path"] = ".github/workflows/build.yml"
    assert not await SmokeService().process_run(20)
    assert reporting["writes"] == []


@pytest.mark.asyncio
async def test_reconciliation_recovers_missed_webhook_once(reporting):
    assert await SmokeService().reconcile() == 1
    assert await SmokeService().reconcile() == 0
    assert len(reporting["writes"]) == 1
    reporting["attempt"] = 2
    assert await SmokeService().reconcile() == 1
    assert [method for method, _ in reporting["writes"]] == ["post", "patch"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("source_sha", "b" * 40), ("app_ref", "app/org.other.App/x86_64/test")],
)
async def test_mismatched_tested_identity_is_reported_as_infrastructure_error(
    reporting, field, value
):
    setattr(reporting["artifact"], field, value)
    await SmokeService().process_run(20)
    body = reporting["writes"][0][1]
    assert "infrastructure error" in body
    assert "differs from" in body
    assert "Download screenshots" not in body
