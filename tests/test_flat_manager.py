import pytest
from unittest.mock import MagicMock
from httpx import HTTPStatusError, TimeoutException, NetworkError

from app.utils.flat_manager import FlatManagerClient, JobStatus, JobKind


@pytest.fixture
def flat_manager_client():
    return FlatManagerClient(
        url="https://test.flathub.org", token="test_token", timeout=30.0
    )


class TestCreateBuild:
    @pytest.mark.asyncio
    async def test_create_build_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "post",
            json_data={"id": "12345", "status": "new", "repo": "stable"},
        )

        with mock_httpx.patch():
            result = await flat_manager_client.create_build(
                "stable", "https://build.log/url"
            )

        assert result["id"] == "12345"
        assert result["status"] == "new"
        assert result["repo"] == "stable"

        mock_httpx.post.assert_called_once_with(
            "https://test.flathub.org/api/v1/build",
            json={
                "repo": "stable",
                "build-log-url": "https://build.log/url",
            },
            headers={"Authorization": "Bearer test_token"},
            timeout=30.0,
        )

    @pytest.mark.asyncio
    async def test_create_build_http_error(self, flat_manager_client, mock_httpx):
        error_response = MagicMock()
        error_response.status_code = 403
        error_response.text = "Forbidden: Invalid token"
        mock_httpx.set_response(
            "post",
            status_code=403,
            text="Forbidden: Invalid token",
            raise_for_status=HTTPStatusError(
                "Forbidden", request=MagicMock(), response=error_response
            ),
        )

        with mock_httpx.patch():
            with pytest.raises(HTTPStatusError) as exc_info:
                await flat_manager_client.create_build(
                    "stable", "https://build.log/url"
                )

        assert exc_info.value.response.status_code == 403

    @pytest.mark.asyncio
    async def test_create_build_timeout_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "post", side_effect=TimeoutException("Request timed out")
        )

        with mock_httpx.patch():
            with pytest.raises(TimeoutException) as exc_info:
                await flat_manager_client.create_build(
                    "stable", "https://build.log/url"
                )

        assert "Request timed out" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_build_network_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("post", side_effect=NetworkError("Network unreachable"))

        with mock_httpx.patch():
            with pytest.raises(NetworkError) as exc_info:
                await flat_manager_client.create_build(
                    "stable", "https://build.log/url"
                )

        assert "Network unreachable" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_build_unexpected_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("post", side_effect=ValueError("Unexpected error"))

        with mock_httpx.patch():
            with pytest.raises(ValueError) as exc_info:
                await flat_manager_client.create_build(
                    "stable", "https://build.log/url"
                )

        assert "Unexpected error" in str(exc_info.value)


class TestGetBuildUrl:
    @pytest.mark.parametrize(
        ("input_value", "expected"),
        [
            ("12345", "https://test.flathub.org/api/v1/build/12345"),
            (
                "http://test.flathub.org/build/67890",
                "https://test.flathub.org/api/v1/build/67890",
            ),
            (
                "https://test.flathub.org/build/67890",
                "https://test.flathub.org/api/v1/build/67890",
            ),
            (
                "https://test.flathub.org/build/67890/",
                "https://test.flathub.org/api/v1/build/67890",
            ),
            (
                "https://test.flathub.org/api/v1/build/12345",
                "https://test.flathub.org/api/v1/build/12345",
            ),
            ("https://test.flathub.org", "https://test.flathub.org/api/v1/build/"),
            (12345, "https://test.flathub.org/api/v1/build/12345"),
        ],
        ids=[
            "id",
            "http_url",
            "https_url",
            "trailing_slash",
            "complex_path",
            "empty_path",
            "non_string_id",
        ],
    )
    def test_get_build_url(self, flat_manager_client, input_value, expected):
        assert flat_manager_client.get_build_url(input_value) == expected


class TestCommit:
    @pytest.mark.asyncio
    async def test_commit_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("post")

        with mock_httpx.patch():
            await flat_manager_client.commit("12345")

        mock_httpx.post.assert_called_once_with(
            "https://test.flathub.org/api/v1/build/12345/commit",
            headers={"Authorization": "Bearer test_token"},
            json={
                "endoflife": None,
                "endoflife_rebase": None,
            },
            timeout=30.0,
        )

    @pytest.mark.asyncio
    async def test_commit_with_end_of_life(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("post")

        with mock_httpx.patch():
            await flat_manager_client.commit(
                "12345",
                end_of_life="This application has been replaced by org.new.app.",
                end_of_life_rebase="org.new.app",
            )

        mock_httpx.post.assert_called_once_with(
            "https://test.flathub.org/api/v1/build/12345/commit",
            headers={"Authorization": "Bearer test_token"},
            json={
                "endoflife": "This application has been replaced by org.new.app.",
                "endoflife_rebase": "org.new.app",
            },
            timeout=30.0,
        )

    @pytest.mark.asyncio
    async def test_commit_http_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "post",
            raise_for_status=HTTPStatusError(
                "Bad Request", request=MagicMock(), response=MagicMock()
            ),
        )

        with mock_httpx.patch():
            with pytest.raises(HTTPStatusError):
                await flat_manager_client.commit("12345")


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("post")

        with mock_httpx.patch():
            await flat_manager_client.publish("12345")

        mock_httpx.post.assert_called_once_with(
            "https://test.flathub.org/api/v1/build/12345/publish",
            headers={"Authorization": "Bearer test_token"},
            json={},
            timeout=30.0,
        )

    @pytest.mark.asyncio
    async def test_publish_http_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "post",
            raise_for_status=HTTPStatusError(
                "Not Found", request=MagicMock(), response=MagicMock()
            ),
        )

        with mock_httpx.patch():
            with pytest.raises(HTTPStatusError):
                await flat_manager_client.publish("12345")


class TestPurge:
    @pytest.mark.asyncio
    async def test_purge_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("post")

        with mock_httpx.patch():
            await flat_manager_client.purge("12345")

        mock_httpx.post.assert_called_once_with(
            "https://test.flathub.org/api/v1/build/12345/purge",
            headers={"Authorization": "Bearer test_token"},
            json={},
            timeout=30.0,
        )

    @pytest.mark.asyncio
    async def test_purge_http_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "post",
            raise_for_status=HTTPStatusError(
                "Forbidden", request=MagicMock(), response=MagicMock()
            ),
        )

        with mock_httpx.patch():
            with pytest.raises(HTTPStatusError):
                await flat_manager_client.purge("12345")


class TestRepublish:
    @pytest.mark.asyncio
    async def test_republish_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("post", json_data={"status": "ok"})

        with mock_httpx.patch():
            result = await flat_manager_client.republish(
                "stable",
                "org.test.App",
                end_of_life="This application has been replaced by org.test.NewApp.",
                end_of_life_rebase="org.test.NewApp",
            )

        assert result == {"status": "ok"}

        mock_httpx.post.assert_called_once_with(
            "https://test.flathub.org/api/v1/repo/stable/republish",
            headers={"Authorization": "Bearer test_token"},
            json={
                "app": "org.test.App",
                "endoflife": "This application has been replaced by org.test.NewApp.",
                "endoflife_rebase": "org.test.NewApp",
            },
            timeout=30.0,
        )


class TestGetJob:
    @pytest.mark.asyncio
    async def test_get_job_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "request",
            json_data={
                "id": 123,
                "kind": JobKind.COMMIT,
                "status": JobStatus.ENDED,
                "repo": "stable",
                "contents": "{}",
                "results": "success",
                "log": "Job completed successfully",
            },
        )

        with mock_httpx.patch():
            result = await flat_manager_client.get_job(123)

        assert result["id"] == 123
        assert result["kind"] == JobKind.COMMIT
        assert result["status"] == JobStatus.ENDED
        assert result["repo"] == "stable"

        mock_httpx.request.assert_called_once_with(
            "GET",
            "https://test.flathub.org/api/v1/job/123",
            headers={"Authorization": "Bearer test_token"},
            json={"log_offset": None},
            timeout=30.0,
        )

    @pytest.mark.asyncio
    async def test_get_job_http_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "request",
            raise_for_status=HTTPStatusError(
                "Not Found", request=MagicMock(), response=MagicMock()
            ),
        )

        with mock_httpx.patch():
            with pytest.raises(HTTPStatusError):
                await flat_manager_client.get_job(999)


class TestMiscellaneous:
    @pytest.mark.asyncio
    async def test_create_token_subset_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "post",
            json_data={
                "token": "new_token_12345",
                "sub": "build/12345",
                "scope": ["upload"],
            },
        )

        with mock_httpx.patch():
            result = await flat_manager_client.create_token_subset(
                "12345", "org.test.App"
            )

        assert result == "new_token_12345"

        mock_httpx.post.assert_called_once_with(
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
    async def test_get_build_info_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "get",
            json_data={
                "id": "12345",
                "repo": "stable",
                "status": "committed",
                "extra_data": {"commit_job_id": 123},
            },
        )

        with mock_httpx.patch():
            result = await flat_manager_client.get_build_info("12345")

        assert result["id"] == "12345"
        assert result["repo"] == "stable"
        assert result["status"] == "committed"

        mock_httpx.get.assert_called_once_with(
            "https://test.flathub.org/api/v1/build/12345/extended",
            headers={"Authorization": "Bearer test_token"},
            timeout=30.0,
        )

    def test_get_flatpakref_url(self, flat_manager_client):
        result = flat_manager_client.get_flatpakref_url("12345", "org.test.App")
        assert (
            result == "https://dl.flathub.org/build-repo/12345/org.test.App.flatpakref"
        )
