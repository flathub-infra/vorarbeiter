import pytest
import zipfile
from io import BytesIO
from unittest.mock import patch, MagicMock

from app.services.github_actions import GitHubActionsService


@pytest.fixture
def github_actions_service():
    return GitHubActionsService()


@pytest.fixture
def mock_httpx_get():
    with patch("httpx.AsyncClient.get") as mock_get:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        yield mock_get, mock_response


@pytest.fixture
def sample_provider_data():
    return {
        "owner": "flathub",
        "repo": "actions",
        "run_id": "12345",
        "workflow_id": "build.yml",
    }


@pytest.fixture
def cancelled_run_response():
    return {
        "id": 12345,
        "status": "completed",
        "conclusion": "cancelled",
        "run_attempt": 1,
        "created_at": "2023-01-01T00:00:00Z",
        "updated_at": "2023-01-01T00:05:00Z",
    }


@pytest.fixture
def cancelled_retry_run_response():
    return {
        "id": 12345,
        "status": "completed",
        "conclusion": "cancelled",
        "run_attempt": 2,
        "created_at": "2023-01-01T00:00:00Z",
        "updated_at": "2023-01-01T00:05:00Z",
    }


@pytest.fixture
def failed_run_response():
    return {
        "id": 12345,
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
        "created_at": "2023-01-01T00:00:00Z",
        "updated_at": "2023-01-01T00:05:00Z",
    }


@pytest.fixture
def log_zip_with_cancellation():
    """Create a ZIP file containing log with cancellation message."""
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        zip_file.writestr(
            "job1.txt", "Starting job...\nThe operation was canceled.\nCleanup..."
        )
        zip_file.writestr("job2.txt", "Another job log without cancellation")
    return zip_buffer.getvalue()


@pytest.fixture
def log_zip_without_cancellation():
    """Create a ZIP file containing log without cancellation message."""
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        zip_file.writestr(
            "job1.txt", "Starting job...\nBuild failed due to errors\nCleanup..."
        )
        zip_file.writestr("job2.txt", "Another job log")
    return zip_buffer.getvalue()


@pytest.fixture
def cancelled_annotations():
    """Create annotations list with cancellation message."""
    return [
        {
            "message": "The operation was canceled.",
            "annotation_level": "failure",
        }
    ]


@pytest.fixture
def normal_failure_annotations():
    """Create annotations list without cancellation message."""
    return [
        {
            "message": "Build failed due to compilation errors",
            "annotation_level": "failure",
        }
    ]


@pytest.fixture
def empty_annotations():
    """Create empty annotations list."""
    return []


@pytest.mark.asyncio
async def test_get_workflow_run_details_success(
    github_actions_service, mock_httpx_get, cancelled_run_response
):
    """Test successful workflow run details retrieval."""
    mock_get, mock_response = mock_httpx_get
    mock_response.json.return_value = cancelled_run_response

    result = await github_actions_service.get_workflow_run_details(
        "owner", "repo", 12345
    )

    assert result == cancelled_run_response
    mock_get.assert_called_once_with("/repos/owner/repo/actions/runs/12345")


@pytest.mark.asyncio
async def test_get_workflow_run_details_http_error(
    github_actions_service, mock_httpx_get
):
    """Test workflow run details retrieval with HTTP error."""
    mock_get, mock_response = mock_httpx_get
    mock_response.raise_for_status.side_effect = Exception("HTTP 404")

    result = await github_actions_service.get_workflow_run_details(
        "owner", "repo", 12345
    )

    assert result is None


@pytest.mark.asyncio
async def test_get_workflow_run_details_exception(
    github_actions_service, mock_httpx_get
):
    """Test workflow run details retrieval with unexpected exception."""
    mock_get, mock_response = mock_httpx_get
    mock_get.side_effect = Exception("Unexpected error")

    result = await github_actions_service.get_workflow_run_details(
        "owner", "repo", 12345
    )

    assert result is None


@pytest.mark.asyncio
async def test_download_run_logs_success(
    github_actions_service, mock_httpx_get, log_zip_with_cancellation
):
    """Test successful log download and extraction."""
    mock_get, mock_response = mock_httpx_get
    mock_response.content = log_zip_with_cancellation

    result = await github_actions_service.download_run_logs("owner", "repo", 12345)

    assert "The operation was canceled." in result
    assert "Starting job..." in result
    mock_get.assert_called_once_with("/repos/owner/repo/actions/runs/12345/logs")


@pytest.mark.asyncio
async def test_download_run_logs_without_cancellation(
    github_actions_service, mock_httpx_get, log_zip_without_cancellation
):
    """Test log download without cancellation message."""
    mock_get, mock_response = mock_httpx_get
    mock_response.content = log_zip_without_cancellation

    result = await github_actions_service.download_run_logs("owner", "repo", 12345)

    assert "The operation was canceled." not in result
    assert "Build failed due to errors" in result


@pytest.mark.asyncio
async def test_download_run_logs_http_error(github_actions_service, mock_httpx_get):
    """Test log download with HTTP error."""
    mock_get, mock_response = mock_httpx_get
    mock_response.raise_for_status.side_effect = Exception("HTTP 404")

    result = await github_actions_service.download_run_logs("owner", "repo", 12345)

    assert result is None


@pytest.mark.asyncio
async def test_download_run_logs_exception(github_actions_service, mock_httpx_get):
    """Test log download with unexpected exception."""
    mock_get, mock_response = mock_httpx_get
    mock_get.side_effect = Exception("Unexpected error")

    result = await github_actions_service.download_run_logs("owner", "repo", 12345)

    assert result is None


@pytest.mark.asyncio
async def test_check_run_was_cancelled_via_api(
    github_actions_service, sample_provider_data, cancelled_annotations
):
    """Test cancellation detection via check-run annotations."""
    with (
        patch.object(
            github_actions_service, "get_workflow_run_details"
        ) as mock_get_details,
        patch(
            "app.services.github_actions.get_check_run_annotations"
        ) as mock_get_annotations,
    ):
        mock_get_details.return_value = {"run_attempt": 1}
        mock_get_annotations.return_value = cancelled_annotations

        result = await github_actions_service.check_run_was_cancelled(
            sample_provider_data
        )

        assert result is True
        mock_get_details.assert_called_once_with("flathub", "actions", 12345)
        mock_get_annotations.assert_called_once_with("flathub", "actions", 12345)


@pytest.mark.asyncio
async def test_check_run_was_cancelled_via_logs(
    github_actions_service, sample_provider_data, cancelled_annotations
):
    """Test cancellation detection via check-run annotations."""
    with (
        patch.object(
            github_actions_service, "get_workflow_run_details"
        ) as mock_get_details,
        patch(
            "app.services.github_actions.get_check_run_annotations"
        ) as mock_get_annotations,
    ):
        mock_get_details.return_value = {"run_attempt": 1}
        mock_get_annotations.return_value = cancelled_annotations

        result = await github_actions_service.check_run_was_cancelled(
            sample_provider_data
        )

        assert result is True
        mock_get_details.assert_called_once_with("flathub", "actions", 12345)
        mock_get_annotations.assert_called_once_with("flathub", "actions", 12345)


@pytest.mark.asyncio
async def test_check_run_was_cancelled_not_cancelled(
    github_actions_service, sample_provider_data, normal_failure_annotations
):
    """Test when run was not cancelled."""
    with (
        patch.object(
            github_actions_service, "get_workflow_run_details"
        ) as mock_get_details,
        patch(
            "app.services.github_actions.get_check_run_annotations"
        ) as mock_get_annotations,
    ):
        mock_get_details.return_value = {"run_attempt": 1}
        mock_get_annotations.return_value = normal_failure_annotations

        result = await github_actions_service.check_run_was_cancelled(
            sample_provider_data
        )

        assert result is False


@pytest.mark.asyncio
async def test_check_run_was_cancelled_missing_fields(github_actions_service):
    """Test cancellation check with missing provider data fields."""
    # Missing run_id
    result = await github_actions_service.check_run_was_cancelled(
        {"owner": "flathub", "repo": "actions"}
    )
    assert result is False

    # Missing owner
    result = await github_actions_service.check_run_was_cancelled(
        {"repo": "actions", "run_id": "12345"}
    )
    assert result is False

    # Missing repo
    result = await github_actions_service.check_run_was_cancelled(
        {"owner": "flathub", "run_id": "12345"}
    )
    assert result is False

    # Empty provider_data
    result = await github_actions_service.check_run_was_cancelled({})
    assert result is False


@pytest.mark.asyncio
async def test_check_run_was_cancelled_invalid_run_id(github_actions_service):
    """Test cancellation check with invalid run_id format."""
    provider_data = {"owner": "flathub", "repo": "actions", "run_id": "invalid"}

    result = await github_actions_service.check_run_was_cancelled(provider_data)

    assert result is False


@pytest.mark.asyncio
async def test_check_run_was_cancelled_api_failures(
    github_actions_service, sample_provider_data
):
    """Test cancellation check when API calls fail."""
    with (
        patch.object(
            github_actions_service, "get_workflow_run_details"
        ) as mock_get_details,
        patch(
            "app.services.github_actions.get_check_run_annotations"
        ) as mock_get_annotations,
    ):
        mock_get_details.return_value = None
        mock_get_annotations.return_value = None

        result = await github_actions_service.check_run_was_cancelled(
            sample_provider_data
        )

        assert result is False


@pytest.mark.asyncio
async def test_check_run_was_cancelled_logs_fallback(
    github_actions_service, sample_provider_data, cancelled_annotations
):
    """Test annotations check when API details fail (annotations are always checked)."""
    with (
        patch.object(
            github_actions_service, "get_workflow_run_details"
        ) as mock_get_details,
        patch(
            "app.services.github_actions.get_check_run_annotations"
        ) as mock_get_annotations,
    ):
        mock_get_details.return_value = None
        mock_get_annotations.return_value = cancelled_annotations

        result = await github_actions_service.check_run_was_cancelled(
            sample_provider_data
        )

        assert result is True
        mock_get_annotations.assert_called_once_with("flathub", "actions", 12345)


@pytest.mark.asyncio
async def test_check_run_was_cancelled_retry_attempt_via_api(
    github_actions_service, sample_provider_data, cancelled_retry_run_response
):
    """Test that cancelled retry attempts are treated as failures via API."""
    with patch.object(
        github_actions_service, "get_workflow_run_details"
    ) as mock_get_details:
        mock_get_details.return_value = cancelled_retry_run_response

        result = await github_actions_service.check_run_was_cancelled(
            sample_provider_data
        )

        assert result is False
        mock_get_details.assert_called_once_with("flathub", "actions", 12345)


@pytest.mark.asyncio
async def test_check_run_was_cancelled_retry_attempt_via_logs(
    github_actions_service, sample_provider_data, cancelled_retry_run_response
):
    """Test that cancelled retry attempts are treated as failures via logs."""
    with patch.object(
        github_actions_service, "get_workflow_run_details"
    ) as mock_get_details:
        mock_get_details.return_value = cancelled_retry_run_response

        result = await github_actions_service.check_run_was_cancelled(
            sample_provider_data
        )

        assert result is False
        mock_get_details.assert_called_once_with("flathub", "actions", 12345)


@pytest.mark.asyncio
async def test_check_run_was_cancelled_missing_run_attempt_defaults_to_1(
    github_actions_service, sample_provider_data, cancelled_annotations
):
    """Test that missing run_attempt defaults to 1 and is treated as cancellation."""
    with (
        patch.object(
            github_actions_service, "get_workflow_run_details"
        ) as mock_get_details,
        patch(
            "app.services.github_actions.get_check_run_annotations"
        ) as mock_get_annotations,
    ):
        # Response without run_attempt field
        mock_get_details.return_value = {
            "id": 12345,
            "status": "completed",
        }
        mock_get_annotations.return_value = cancelled_annotations

        result = await github_actions_service.check_run_was_cancelled(
            sample_provider_data
        )

        assert result is True
        mock_get_details.assert_called_once_with("flathub", "actions", 12345)
        mock_get_annotations.assert_called_once_with("flathub", "actions", 12345)


@pytest.mark.asyncio
async def test_check_run_was_cancelled_retry_via_logs_only(
    github_actions_service, sample_provider_data, cancelled_annotations
):
    """Test retry detection when only annotations are available (API details failed)."""
    with (
        patch.object(
            github_actions_service, "get_workflow_run_details"
        ) as mock_get_details,
        patch(
            "app.services.github_actions.get_check_run_annotations"
        ) as mock_get_annotations,
    ):
        mock_get_details.return_value = None
        mock_get_annotations.return_value = cancelled_annotations

        result = await github_actions_service.check_run_was_cancelled(
            sample_provider_data
        )

        assert result is True
        mock_get_details.assert_called_once_with("flathub", "actions", 12345)
        mock_get_annotations.assert_called_once_with("flathub", "actions", 12345)
