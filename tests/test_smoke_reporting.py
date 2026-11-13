import base64
import io
import json
import uuid
import zipfile
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import pytest
import pytest_asyncio

from app.models import Pipeline, PipelineStatus
from app.services.smoke import SmokeService
from app.services.smoke_artifacts import API, SmokeArtifact, parse_result_archive
from app.smoke import collect_results


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
        "conclusion": "success",
        "inputs": [{"id": 30, "name": "smoke-input-1-x86_64"}],
        "missing_result": False,
        "recipe": "ci/screenshots.yml",
    }

    async def request(method, url, **kwargs):
        if url == f"{API}/actions/runs/20":
            data = {
                "id": 20,
                "run_attempt": state["attempt"],
                "status": "completed",
                "conclusion": state["conclusion"],
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
            data = {"artifacts": state["inputs"]}
        elif url.endswith("/actions/runs/20/artifacts"):
            data = {
                "artifacts": [
                    {
                        "id": 40,
                        "name": f"smoke-result-{state['attempt']}",
                        "expired": False,
                    }
                ]
                if not state["missing_result"]
                else []
            }
        elif url.endswith("/contents/flathub.json"):
            assert kwargs["params"] == {"ref": "a" * 40}
            config = {"enabled": True}
            if state["recipe"]:
                config["screenshot-recipe"] = state["recipe"]
            data = {
                "content": base64.b64encode(
                    json.dumps({"smoke-test": config}).encode()
                ).decode()
            }
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
        return httpx.Response(200, json=data, request=httpx.Request(method, url))

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
@pytest.mark.parametrize("status", ["passed", "failed"])
async def test_checks_comment_once_with_preview_without_changing_publication(
    reporting, status
):
    reporting["artifact"].summary.update(
        verify=status,
        screenshots=status,
        previews=[{"path": "screenshots/overview.png", "caption": "Overview"}],
    )
    await SmokeService().process_run(20)
    await SmokeService().process_run(20)
    assert len(reporting["writes"]) == 1
    method, body = reporting["writes"][0]
    assert method == "post"
    assert "Application startup and screenshot checks" in body and status in body
    assert "![Overview](" in body and "/40/screenshots/overview.png)" in body
    assert (
        "https://github.com/flathub-infra/vorarbeiter/actions/runs/20/artifacts/40"
        in body
    )
    assert "These checks do not block publication" in body
    assert "<script>" not in body and "@someone" not in body


@pytest.mark.asyncio
async def test_rerun_updates_comment_instead_of_posting_again(reporting):
    await SmokeService().process_run(20)
    reporting["attempt"] = 2
    await SmokeService().process_run(20)
    assert [method for method, _ in reporting["writes"]] == ["post", "patch"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "unreadable", "inputs"])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("recipe", ["ci/screenshots.yml", None])
async def test_missing_results_preserve_requested_modes(
    reporting, failure, cancelled, recipe
):
    reporting["recipe"] = recipe
    reporting["missing_result"] = failure == "missing"
    if failure == "inputs":
        reporting["inputs"] = []
    reporting["conclusion"] = "cancelled" if cancelled else "failure"
    with patch(
        "app.services.smoke.download_artifact", side_effect=ValueError("bad artifact")
    ):
        await SmokeService().process_run(20)
    body = reporting["writes"][0][1]
    status = "cancelled" if cancelled else "infrastructure error"
    assert f"Startup verification: **{status}**" in body
    assert f"Screenshot capture: **{status if recipe else 'skipped'}**" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("recipe", ["ci/screenshots.yml", None])
@pytest.mark.parametrize("corrupt", [False, True])
async def test_fallback_report_recovers_requested_modes(
    reporting, tmp_path, cancelled, recipe, corrupt
):
    source = tmp_path / "input"
    source.mkdir()
    if corrupt:
        (source / "request.json").write_text("not JSON")
    output = tmp_path / "report"
    collect_results(
        source,
        tmp_path / "results",
        output,
        {"verify": "skipped", "screenshots": "skipped"},
    )
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.write(output / "report.json", "report.json")
    parsed = parse_result_archive(data.getvalue())
    reporting["recipe"] = recipe
    reporting["conclusion"] = "cancelled" if cancelled else "failure"
    with patch("app.services.smoke.download_artifact", return_value=parsed):
        await SmokeService().process_run(20)
    body = reporting["writes"][0][1]
    status = "cancelled" if cancelled else "infrastructure error"
    assert f"Startup verification: **{status}**" in body
    assert f"Screenshot capture: **{status if recipe else 'skipped'}**" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("startup", ["passed", "failed", "infrastructure_error"])
async def test_cancelled_capture_preserves_completed_startup(reporting, startup):
    reporting["conclusion"] = "cancelled"
    reporting["artifact"].summary.update(verify=startup, screenshots="cancelled")
    await SmokeService().process_run(20)
    body = reporting["writes"][0][1]
    assert f"Startup verification: **{startup.replace('_', ' ')}**" in body
    assert "Screenshot capture: **cancelled**" in body


@pytest.mark.asyncio
async def test_cancellation_marks_only_unexecuted_modes(reporting):
    reporting["conclusion"] = "cancelled"
    reporting["artifact"].summary.update(
        verify="passed",
        screenshots="infrastructure_error",
        unexecuted_modes=["screenshots"],
    )
    await SmokeService().process_run(20)
    body = reporting["writes"][0][1]
    assert "Startup verification: **passed**" in body
    assert "Screenshot capture: **cancelled**" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt,skipped", [(1, True), (2, False)])
async def test_dual_arch_build_reports_only_current_attempt_skip(
    reporting, attempt, skipped
):
    reporting["inputs"].append({"id": 31, "name": f"smoke-input-{attempt}-aarch64"})
    await SmokeService().process_run(20)
    body = reporting["writes"][0][1]
    assert ("aarch64: **skipped**" in body) is skipped


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
