import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import HTTPStatusError, TimeoutException, NetworkError

from app.utils.flat_manager import FlatManagerClient, JobStatus, JobKind


@pytest.fixture
def flat_manager_client():
    return FlatManagerClient(
        url="https://test.flathub.org", token="test_token", timeout=30.0
    )


@pytest.fixture
def mock_response():
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    return response


class TestCreateBuild:
    @pytest.mark.asyncio
    async def test_create_build_success(self, flat_manager_client, mock_response):
        mock_response.json.return_value = {
            "id": "12345",
            "status": "new",
            "repo": "stable",
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response

            result = await flat_manager_client.create_build(
                "stable", "https://build.log/url"
            )

            assert result["id"] == "12345"
            assert result["status"] == "new"
            assert result["repo"] == "stable"

            mock_client.post.assert_called_once_with(
                "https://test.flathub.org/api/v1/build",
                json={
                    "repo": "stable",
                    "build-log-url": "https://build.log/url",
                },
                headers={"Authorization": "Bearer test_token"},
                timeout=30.0,
            )

    @pytest.mark.asyncio
    async def test_create_build_http_error(self, flat_manager_client, mock_response):
        mock_response.status_code = 403
        mock_response.text = "Forbidden: Invalid token"
        mock_response.raise_for_status.side_effect = HTTPStatusError(
            "Forbidden", request=MagicMock(), response=mock_response
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response

            with pytest.raises(HTTPStatusError) as exc_info:
                await flat_manager_client.create_build(
                    "stable", "https://build.log/url"
                )

            assert exc_info.value.response.status_code == 403

    @pytest.mark.asyncio
    async def test_create_build_timeout_error(self, flat_manager_client):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.side_effect = TimeoutException("Request timed out")

            with pytest.raises(TimeoutException) as exc_info:
                await flat_manager_client.create_build(
                    "stable", "https://build.log/url"
                )

            assert "Request timed out" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_build_network_error(self, flat_manager_client):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.side_effect = NetworkError("Network unreachable")

            with pytest.raises(NetworkError) as exc_info:
                await flat_manager_client.create_build(
                    "stable", "https://build.log/url"
                )

            assert "Network unreachable" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_build_unexpected_error(self, flat_manager_client):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.side_effect = ValueError("Unexpected error")

            with pytest.raises(ValueError) as exc_info:
                await flat_manager_client.create_build(
                    "stable", "https://build.log/url"
                )

            assert "Unexpected error" in str(exc_info.value)


class TestGetBuildUrl:
    def test_get_build_url_with_id(self, flat_manager_client):
        result = flat_manager_client.get_build_url("12345")
        assert result == "https://test.flathub.org/api/v1/build/12345"

    def test_get_build_url_with_http_url(self, flat_manager_client):
        url = "http://test.flathub.org/build/67890"
        result = flat_manager_client.get_build_url(url)
        assert result == "https://test.flathub.org/api/v1/build/67890"

    def test_get_build_url_with_https_url(self, flat_manager_client):
        url = "https://test.flathub.org/build/67890"
        result = flat_manager_client.get_build_url(url)
        assert result == "https://test.flathub.org/api/v1/build/67890"

    def test_get_build_url_with_trailing_slash(self, flat_manager_client):
        url = "https://test.flathub.org/build/67890/"
        result = flat_manager_client.get_build_url(url)
        assert result == "https://test.flathub.org/api/v1/build/67890"

    def test_get_build_url_with_complex_path(self, flat_manager_client):
        url = "https://test.flathub.org/api/v1/build/12345"
        result = flat_manager_client.get_build_url(url)
        assert result == "https://test.flathub.org/api/v1/build/12345"

    def test_get_build_url_with_empty_path(self, flat_manager_client):
        url = "https://test.flathub.org"
        result = flat_manager_client.get_build_url(url)
        assert result == "https://test.flathub.org/api/v1/build/"

    def test_get_build_url_with_non_string_id(self, flat_manager_client):
        result = flat_manager_client.get_build_url(12345)
        assert result == "https://test.flathub.org/api/v1/build/12345"


class TestCommit:
    @pytest.mark.asyncio
    async def test_commit_success(self, flat_manager_client, mock_response):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response

            await flat_manager_client.commit("12345")

            mock_client.post.assert_called_once_with(
                "https://test.flathub.org/api/v1/build/12345/commit",
                headers={"Authorization": "Bearer test_token"},
                json={
                    "endoflife": None,
                    "endoflife_rebase": None,
                },
                timeout=30.0,
            )

    @pytest.mark.asyncio
    async def test_commit_with_end_of_life(self, flat_manager_client, mock_response):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response

            await flat_manager_client.commit(
                "12345",
                end_of_life="This app is deprecated",
                end_of_life_rebase="org.new.app",
            )

            mock_client.post.assert_called_once_with(
                "https://test.flathub.org/api/v1/build/12345/commit",
                headers={"Authorization": "Bearer test_token"},
                json={
                    "endoflife": "This app is deprecated",
                    "endoflife_rebase": "org.new.app",
                },
                timeout=30.0,
            )

    @pytest.mark.asyncio
    async def test_commit_http_error(self, flat_manager_client, mock_response):
        mock_response.raise_for_status.side_effect = HTTPStatusError(
            "Bad Request", request=MagicMock(), response=mock_response
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response

            with pytest.raises(HTTPStatusError):
                await flat_manager_client.commit("12345")


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_success(self, flat_manager_client, mock_response):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response

            await flat_manager_client.publish("12345")

            mock_client.post.assert_called_once_with(
                "https://test.flathub.org/api/v1/build/12345/publish",
                headers={"Authorization": "Bearer test_token"},
                json={},
                timeout=30.0,
            )

    @pytest.mark.asyncio
    async def test_publish_http_error(self, flat_manager_client, mock_response):
        mock_response.raise_for_status.side_effect = HTTPStatusError(
            "Not Found", request=MagicMock(), response=mock_response
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response

            with pytest.raises(HTTPStatusError):
                await flat_manager_client.publish("12345")


class TestPurge:
    @pytest.mark.asyncio
    async def test_purge_success(self, flat_manager_client, mock_response):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response

            await flat_manager_client.purge("12345")

            mock_client.post.assert_called_once_with(
                "https://test.flathub.org/api/v1/build/12345/purge",
                headers={"Authorization": "Bearer test_token"},
                json={},
                timeout=30.0,
            )

    @pytest.mark.asyncio
    async def test_purge_http_error(self, flat_manager_client, mock_response):
        mock_response.raise_for_status.side_effect = HTTPStatusError(
            "Forbidden", request=MagicMock(), response=mock_response
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response

            with pytest.raises(HTTPStatusError):
                await flat_manager_client.purge("12345")


class TestGetJob:
    @pytest.mark.asyncio
    async def test_get_job_success(self, flat_manager_client, mock_response):
        mock_response.json.return_value = {
            "id": 123,
            "kind": JobKind.COMMIT,
            "status": JobStatus.ENDED,
            "repo": "stable",
            "contents": "{}",
            "results": "success",
            "log": "Job completed successfully",
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.request.return_value = mock_response

            result = await flat_manager_client.get_job(123)

            assert result["id"] == 123
            assert result["kind"] == JobKind.COMMIT
            assert result["status"] == JobStatus.ENDED
            assert result["repo"] == "stable"

            mock_client.request.assert_called_once_with(
                "GET",
                "https://test.flathub.org/api/v1/job/123",
                headers={"Authorization": "Bearer test_token"},
                json={"log_offset": None},
                timeout=30.0,
            )

    @pytest.mark.asyncio
    async def test_get_job_http_error(self, flat_manager_client, mock_response):
        mock_response.raise_for_status.side_effect = HTTPStatusError(
            "Not Found", request=MagicMock(), response=mock_response
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.request.return_value = mock_response

            with pytest.raises(HTTPStatusError):
                await flat_manager_client.get_job(999)


class TestMiscellaneous:
    @pytest.mark.asyncio
    async def test_create_token_subset_success(
        self, flat_manager_client, mock_response
    ):
        mock_response.json.return_value = {
            "token": "new_token_12345",
            "sub": "build/12345",
            "scope": ["upload"],
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.post.return_value = mock_response

            result = await flat_manager_client.create_token_subset(
                "12345", "org.test.App"
            )

            assert result == "new_token_12345"

            mock_client.post.assert_called_once_with(
                "https://test.flathub.org/api/v1/token_subset",
                json={
                    "name": "upload",
                    "sub": "build/12345",
                    "scope": ["upload"],
                    "prefix": ["org.test.App"],
                    "duration": 24 * 60 * 60,
                },
                headers={"Authorization": "Bearer test_token"},
                timeout=30.0,
            )

    @pytest.mark.asyncio
    async def test_get_build_info_success(self, flat_manager_client, mock_response):
        mock_response.json.return_value = {
            "id": "12345",
            "repo": "stable",
            "status": "committed",
            "extra_data": {"commit_job_id": 123},
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client
            mock_client.get.return_value = mock_response

            result = await flat_manager_client.get_build_info("12345")

            assert result["id"] == "12345"
            assert result["repo"] == "stable"
            assert result["status"] == "committed"

            mock_client.get.assert_called_once_with(
                "https://test.flathub.org/api/v1/build/12345/extended",
                headers={"Authorization": "Bearer test_token"},
                timeout=30.0,
            )

    def test_get_flatpakref_url(self, flat_manager_client):
        result = flat_manager_client.get_flatpakref_url("12345", "org.test.App")
        assert (
            result == "https://dl.flathub.org/build-repo/12345/org.test.App.flatpakref"
        )
