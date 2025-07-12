"""Tests for GitHub Actions service new functionality."""

import zipfile
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from app.services.github_actions import GitHubActionsService


@pytest.fixture
def github_service():
    """Create a GitHubActionsService instance for testing."""
    service = GitHubActionsService()
    service.token = "test-token"
    return service


class TestExtractRunIdFromLogUrl:
    """Test extraction of run_id from GitHub Actions log URLs."""

    def test_extract_run_id_from_valid_log_url(self, github_service):
        """Test extraction from valid GitHub Actions log URL."""
        log_url = "https://github.com/flathub/vorarbeiter/actions/runs/12345"
        result = github_service.extract_run_id_from_log_url(log_url)
        assert result == 12345

    def test_extract_run_id_from_log_url_with_path(self, github_service):
        """Test extraction from log URL with additional path components."""
        log_url = "https://github.com/owner/repo/actions/runs/67890/attempts/1"
        result = github_service.extract_run_id_from_log_url(log_url)
        assert result == 67890

    def test_extract_run_id_from_log_url_with_query_params(self, github_service):
        """Test extraction from log URL with query parameters."""
        log_url = (
            "https://github.com/owner/repo/actions/runs/11111?check_suite_focus=true"
        )
        result = github_service.extract_run_id_from_log_url(log_url)
        assert result == 11111

    def test_extract_run_id_from_empty_url(self, github_service):
        """Test handling of empty or None URL."""
        assert github_service.extract_run_id_from_log_url("") is None
        assert github_service.extract_run_id_from_log_url(None) is None

    def test_extract_run_id_from_invalid_url(self, github_service):
        """Test handling of invalid URLs."""
        invalid_urls = [
            "https://example.com/some/other/path",
            "https://github.com/owner/repo",
            "https://github.com/owner/repo/pulls/123",
            "not-a-url",
            "github.com/owner/repo/actions/runs/not-a-number",
        ]

        for url in invalid_urls:
            assert github_service.extract_run_id_from_log_url(url) is None

    def test_extract_run_id_from_malformed_run_id(self, github_service):
        """Test handling of malformed run_id in URL."""
        log_url = "https://github.com/owner/repo/actions/runs/abc123"
        result = github_service.extract_run_id_from_log_url(log_url)
        assert result is None


class TestFetchRunLogs:
    """Test fetching of GitHub Actions run logs."""

    @pytest.mark.asyncio
    async def test_fetch_run_logs_success(self, github_service):
        """Test successful log fetching and extraction."""
        # Create a mock ZIP file with log content
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("job1.txt", "Log content from job 1\nLine 2\n")
            zip_file.writestr("job2.txt", "Log content from job 2\nAnother line\n")

        zip_content = zip_buffer.getvalue()

        mock_response = MagicMock()
        mock_response.content = zip_content
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value.__aenter__.return_value = mock_client

            result = await github_service.fetch_run_logs("owner", "repo", 12345)

            mock_client.get.assert_called_once_with(
                "/repos/owner/repo/actions/runs/12345/logs"
            )

            assert "Log content from job 1" in result
            assert "Log content from job 2" in result
            assert "Line 2" in result
            assert "Another line" in result

    @pytest.mark.asyncio
    async def test_fetch_run_logs_large_content_truncation(self, github_service):
        """Test that large log content is properly truncated."""
        # Create a log with many lines
        log_lines = [f"Line {i}" for i in range(2000)]
        log_content = "\n".join(log_lines)

        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("job.txt", log_content)

        zip_content = zip_buffer.getvalue()

        mock_response = MagicMock()
        mock_response.content = zip_content
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value.__aenter__.return_value = mock_client

            result = await github_service.fetch_run_logs("owner", "repo", 12345)

            # Should contain only the last 1000 lines
            result_lines = result.split("\n")
            assert len(result_lines) <= 1000
            # Should contain the end of the log
            assert "Line 1999" in result
            # Should not contain the beginning of the log
            assert "Line 0" not in result

    @pytest.mark.asyncio
    async def test_fetch_run_logs_http_error(self, github_service):
        """Test handling of HTTP errors during log fetching."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not found", request=MagicMock(), response=MagicMock()
        )

        with patch.object(github_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value.__aenter__.return_value = mock_client

            result = await github_service.fetch_run_logs("owner", "repo", 12345)

            assert result is None

    @pytest.mark.asyncio
    async def test_fetch_run_logs_invalid_zip(self, github_service):
        """Test handling of invalid ZIP content."""
        mock_response = MagicMock()
        mock_response.content = b"not a valid zip file"
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value.__aenter__.return_value = mock_client

            result = await github_service.fetch_run_logs("owner", "repo", 12345)

            assert result is None

    @pytest.mark.asyncio
    async def test_fetch_run_logs_empty_zip(self, github_service):
        """Test handling of empty ZIP file."""
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w"):
            pass

        zip_content = zip_buffer.getvalue()

        mock_response = MagicMock()
        mock_response.content = zip_content
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value.__aenter__.return_value = mock_client

            result = await github_service.fetch_run_logs("owner", "repo", 12345)

            assert result == ""

    @pytest.mark.asyncio
    async def test_fetch_run_logs_non_text_files(self, github_service):
        """Test handling of ZIP with non-text files."""
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("image.png", b"\x89PNG\r\n\x1a\n")
            zip_file.writestr("log.txt", "Valid log content")
            zip_file.writestr("data.bin", b"\x00\x01\x02\x03")

        zip_content = zip_buffer.getvalue()

        mock_response = MagicMock()
        mock_response.content = zip_content
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value.__aenter__.return_value = mock_client

            result = await github_service.fetch_run_logs("owner", "repo", 12345)

            # Should only include content from .txt files
            assert "Valid log content" in result
            assert len(result.strip()) == len("Valid log content")

    @pytest.mark.asyncio
    async def test_fetch_run_logs_unicode_content(self, github_service):
        """Test handling of Unicode content in logs."""
        unicode_content = "Log with Unicode: 🚀 ✅ ❌ 中文 español"

        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            zip_file.writestr("log.txt", unicode_content.encode("utf-8"))

        zip_content = zip_buffer.getvalue()

        mock_response = MagicMock()
        mock_response.content = zip_content
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value.__aenter__.return_value = mock_client

            result = await github_service.fetch_run_logs("owner", "repo", 12345)

            assert unicode_content in result

    @pytest.mark.asyncio
    async def test_fetch_run_logs_malformed_unicode(self, github_service):
        """Test handling of malformed Unicode content."""
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            # Write some bytes that are not valid UTF-8
            zip_file.writestr("log.txt", b"Valid start\x80\x81\x82 valid end")

        zip_content = zip_buffer.getvalue()

        mock_response = MagicMock()
        mock_response.content = zip_content
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value.__aenter__.return_value = mock_client

            result = await github_service.fetch_run_logs("owner", "repo", 12345)

            # Should handle malformed Unicode gracefully
            assert "Valid start" in result
            assert "valid end" in result


class TestUpdatedDispatchMethod:
    """Test the updated dispatch method without correlation_id."""

    @pytest.mark.asyncio
    async def test_dispatch_without_correlation_id(self, github_service):
        """Test that dispatch no longer includes correlation_id."""
        job_data = {
            "app_id": "org.flathub.Test",
            "params": {
                "owner": "flathub",
                "repo": "actions",
                "workflow_id": "build.yml",
                "inputs": {"test_input": "value"},
            },
        }

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get_client.return_value.__aenter__.return_value = mock_client

            result = await github_service.dispatch("job1", "pipeline1", job_data)

            # Verify the API call was made correctly
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args

            # Check that correlation_id is not in the payload
            import json

            payload = json.loads(call_args[1]["content"])
            inputs = payload["inputs"]

            assert "correlation_id" not in inputs
            assert inputs["app_id"] == "org.flathub.Test"
            assert inputs["test_input"] == "value"

            # Verify return value structure
            assert result["status"] == "dispatched"
            assert result["owner"] == "flathub"
            assert result["repo"] == "actions"
            assert "run_id" not in result  # run_id should not be present initially

    @pytest.mark.asyncio
    async def test_dispatch_with_additional_params(self, github_service):
        """Test dispatch with additional parameters."""
        job_data = {
            "app_id": "org.flathub.Test",
            "params": {
                "owner": "flathub",
                "repo": "actions",
                "workflow_id": "build.yml",
                "inputs": {"base_input": "value"},
                "additional_params": {"extra_param": "extra_value"},
            },
        }

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch.object(github_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_get_client.return_value.__aenter__.return_value = mock_client

            await github_service.dispatch("job1", "pipeline1", job_data)

            call_args = mock_client.post.call_args
            import json

            payload = json.loads(call_args[1]["content"])
            inputs = payload["inputs"]

            assert inputs["base_input"] == "value"
            assert inputs["extra_param"] == "extra_value"
            assert inputs["app_id"] == "org.flathub.Test"
