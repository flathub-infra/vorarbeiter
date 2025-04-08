import pytest
import uuid
from unittest.mock import patch, AsyncMock, MagicMock

from app.providers import ProviderType, GitHubJobProvider, ProviderFactory


@pytest.fixture
def github_config():
    return {"token": "test-token", "base_url": "https://api.github.com"}


@pytest.fixture
def mock_httpx_post():
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response
        yield mock_post


@pytest.mark.asyncio
async def test_github_provider_dispatch(github_config, mock_httpx_post):
    provider = GitHubJobProvider()
    await provider.initialize(github_config)

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

    mock_httpx_post.assert_called_once()
    args, kwargs = mock_httpx_post.call_args
    assert args[0] == "/repos/flathub/actions/actions/workflows/build.yml/dispatches"

    assert result["status"] == "dispatched"
    assert result["job_id"] == job_id
    assert result["pipeline_id"] == pipeline_id
    assert result["owner"] == "flathub"
    assert result["repo"] == "actions"
    assert result["workflow_id"] == "build.yml"
    assert result["ref"] == "main"


@pytest.mark.asyncio
async def test_github_provider_cancel(github_config, mock_httpx_post):
    provider = GitHubJobProvider()
    await provider.initialize(github_config)

    job_id = str(uuid.uuid4())
    provider_data = {"owner": "flathub", "repo": "actions", "run_id": 12345}

    mock_httpx_post.return_value.status_code = 202

    result = await provider.cancel(job_id, provider_data)

    mock_httpx_post.assert_called_once()
    args, kwargs = mock_httpx_post.call_args
    assert args[0] == "/repos/flathub/actions/actions/runs/12345/cancel"

    assert result is True


@pytest.mark.asyncio
async def test_github_provider_cancel_missing_run_id(github_config, mock_httpx_post):
    provider = GitHubJobProvider()
    await provider.initialize(github_config)

    job_id = str(uuid.uuid4())
    provider_data = {"owner": "flathub", "repo": "actions"}

    result = await provider.cancel(job_id, provider_data)

    mock_httpx_post.assert_not_called()
    assert result is False


@pytest.mark.asyncio
async def test_provider_factory_create():
    mock_provider_class = MagicMock()
    mock_provider = AsyncMock(spec=GitHubJobProvider)
    mock_provider_class.return_value = mock_provider

    with patch.dict(
        ProviderFactory._providers, {ProviderType.GITHUB: mock_provider_class}
    ):
        config = {"token": "test-token"}
        provider = await ProviderFactory.create_provider(ProviderType.GITHUB, config)

        assert provider is mock_provider
        mock_provider.initialize.assert_awaited_once_with(config)


@pytest.mark.asyncio
async def test_provider_factory_unsupported_provider():
    with pytest.raises(ValueError) as excinfo:
        invalid_provider_type = MagicMock()
        invalid_provider_type.__str__.return_value = "invalid"

        await ProviderFactory.create_provider(invalid_provider_type, {})

    assert "Unsupported provider type" in str(excinfo.value)
