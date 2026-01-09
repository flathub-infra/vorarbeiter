import pytest
import uuid
from unittest.mock import patch, MagicMock, AsyncMock

from app.services import GitHubActionsService


@pytest.fixture
def github_token():
    return "test-token"


@pytest.fixture
def mock_httpx_client():
    with patch("httpx.AsyncClient") as MockClient:
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        MockClient.return_value.__aenter__.return_value = mock_client_instance
        yield mock_client_instance, mock_response


@pytest.mark.asyncio
async def test_github_provider_dispatch(github_token, mock_httpx_client):
    mock_client, mock_response = mock_httpx_client
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

    result = await provider.dispatch(job_id, pipeline_id, job_data)

    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert (
        "https://api.github.com/repos/flathub/actions/actions/workflows/build.yml/dispatches"
        in args[0]
    )

    assert result["status"] == "dispatched"
    assert result["job_id"] == job_id
    assert result["pipeline_id"] == pipeline_id
    assert result["owner"] == "flathub"
    assert result["repo"] == "actions"
    assert result["workflow_id"] == "build.yml"
    assert result["ref"] == "main"


@pytest.mark.asyncio
async def test_github_provider_cancel(github_token, mock_httpx_client):
    mock_client, mock_response = mock_httpx_client
    mock_response.status_code = 202
    provider = GitHubActionsService()

    job_id = str(uuid.uuid4())
    provider_data = {"owner": "flathub", "repo": "actions", "run_id": 12345}

    result = await provider.cancel(job_id, provider_data)

    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert (
        "https://api.github.com/repos/flathub/actions/actions/runs/12345/cancel"
        in args[0]
    )

    assert result is True


@pytest.mark.asyncio
async def test_github_provider_cancel_missing_run_id(github_token, mock_httpx_client):
    mock_client, _ = mock_httpx_client
    provider = GitHubActionsService()

    job_id = str(uuid.uuid4())
    provider_data = {"owner": "flathub", "repo": "actions"}

    result = await provider.cancel(job_id, provider_data)

    mock_client.post.assert_not_called()
    assert result is False
