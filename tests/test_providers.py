import pytest
import uuid

from app.services import GitHubActionsService


@pytest.fixture
def github_token():
    return "test-token"


@pytest.mark.asyncio
async def test_github_provider_dispatch(github_token, mock_httpx):
    mock_httpx.set_response("request", status_code=204)
    provider = GitHubActionsService()

    job_id = str(uuid.uuid4())
    pipeline_id = str(uuid.uuid4())
    job_data = {
        "app_id": "org.flathub.Test",
        "job_type": "build",
        "params": {
            "owner": "flathub",
            "repo": "actions",
            "workflow_id": "build.yml",
            "ref": "main",
            "inputs": {"flatpak_id": "org.flathub.Test"},
        },
    }

    with mock_httpx.patch():
        result = await provider.dispatch(job_id, pipeline_id, job_data)

    mock_httpx.request.assert_called_once()
    args, _ = mock_httpx.request.call_args
    assert args[0] == "POST"
    assert (
        "https://api.github.com/repos/flathub/actions/actions/workflows/build.yml/dispatches"
        in args[1]
    )

    assert result["status"] == "dispatched"
    assert result["job_id"] == job_id
    assert result["pipeline_id"] == pipeline_id
    assert result["owner"] == "flathub"
    assert result["repo"] == "actions"
    assert result["workflow_id"] == "build.yml"
    assert result["ref"] == "main"


@pytest.mark.asyncio
async def test_github_provider_cancel(github_token, mock_httpx):
    mock_httpx.set_response("request", status_code=202)
    provider = GitHubActionsService()

    job_id = str(uuid.uuid4())
    provider_data = {"owner": "flathub", "repo": "actions", "run_id": 12345}

    with mock_httpx.patch():
        result = await provider.cancel(job_id, provider_data)

    mock_httpx.request.assert_called_once()
    args, _ = mock_httpx.request.call_args
    assert args[0] == "POST"
    assert (
        "https://api.github.com/repos/flathub/actions/actions/runs/12345/cancel"
        in args[1]
    )

    assert result is True


@pytest.mark.asyncio
async def test_github_provider_cancel_missing_run_id(github_token, mock_httpx):
    provider = GitHubActionsService()

    job_id = str(uuid.uuid4())
    provider_data = {"owner": "flathub", "repo": "actions"}

    with mock_httpx.patch():
        result = await provider.cancel(job_id, provider_data)

    mock_httpx.request.assert_not_called()
    assert result is False


@pytest.mark.asyncio
async def test_github_provider_get_workflow_run_jobs(github_token, mock_httpx):
    mock_httpx.set_response(
        "request",
        status_code=200,
        json_data={"jobs": [{"name": "build-x86_64", "status": "in_progress"}]},
    )
    provider = GitHubActionsService()

    with mock_httpx.patch():
        result = await provider.get_workflow_run_jobs("flathub", "actions", 12345)

    mock_httpx.request.assert_called_once()
    args, _ = mock_httpx.request.call_args
    assert args[0] == "GET"
    assert (
        "https://api.github.com/repos/flathub/actions/actions/runs/12345/jobs"
        in args[1]
    )
    assert result == [{"name": "build-x86_64", "status": "in_progress"}]
