import pytest
from unittest.mock import AsyncMock, MagicMock
from httpxyz import HTTPStatusError, NetworkError, Request, Response, TimeoutException

from app.utils.flat_manager import FlatManagerClient, JobStatus, JobKind


@pytest.fixture
def flat_manager_client(mock_httpx):
    client = FlatManagerClient(
        url="https://test.flathub.org", token="test_token", timeout=30.0
    )
    client.client = mock_httpx._client
    return client


class TestRequestTimeout:
    @pytest.mark.asyncio
    async def test_build_info_recovers_from_http_timeout(
        self, flat_manager_client, monkeypatch
    ):
        request = Request(
            "GET", flat_manager_client.get_build_url(331683) + "/extended"
        )
        flat_manager_client.client.request.side_effect = [
            Response(408, request=request),
            Response(200, request=request, json={"build": {"commit_job_id": 123}}),
        ]
        sleep = AsyncMock()
        monkeypatch.setattr("asyncio.sleep", sleep)

        result = await flat_manager_client.get_build_info(331683)

        assert result == {"build": {"commit_job_id": 123}}
        assert flat_manager_client.client.request.call_count == 2
        sleep.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "status", "attempts"),
        [("GET", 408, 3), ("GET", 403, 1), ("POST", 408, 1)],
    )
    async def test_failure_is_propagated(
        self, flat_manager_client, monkeypatch, method, status, attempts
    ):
        url = flat_manager_client.get_build_url(331683)
        response = Response(status, request=Request(method, url))
        flat_manager_client.client.request.return_value = response
        sleep = AsyncMock()
        monkeypatch.setattr("asyncio.sleep", sleep)

        with pytest.raises(HTTPStatusError) as exc_info:
            await flat_manager_client._request(method, url)

        assert exc_info.value.response is response
        assert flat_manager_client.client.request.call_count == attempts
        assert sleep.await_count == attempts - 1


class TestCreateBuild:
    @pytest.mark.asyncio
    async def test_create_build_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "request",
            json_data={"id": "12345", "status": "new", "repo": "stable"},
        )
        result = await flat_manager_client.create_build(
            "stable", "https://build.log/url"
        )

        assert result["id"] == "12345"
        assert result["status"] == "new"
        assert result["repo"] == "stable"

        mock_httpx.request.assert_called_once_with(
            "POST",
            "https://test.flathub.org/api/v1/build",
            headers={"Authorization": "Bearer test_token"},
            timeout=30.0,
            json={
                "repo": "stable",
                "build-log-url": "https://build.log/url",
            },
        )

    @pytest.mark.asyncio
    async def test_create_build_http_error(self, flat_manager_client, mock_httpx):
        error_response = MagicMock()
        error_response.status_code = 403
        error_response.text = "Forbidden: Invalid token"
        mock_httpx.set_response(
            "request",
            status_code=403,
            text="Forbidden: Invalid token",
            raise_for_status=HTTPStatusError(
                "Forbidden", request=MagicMock(), response=error_response
            ),
        )
        with pytest.raises(HTTPStatusError) as exc_info:
            await flat_manager_client.create_build("stable", "https://build.log/url")

        assert exc_info.value.response.status_code == 403

    @pytest.mark.asyncio
    async def test_create_build_timeout_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "request", side_effect=TimeoutException("Request timed out")
        )
        with pytest.raises(TimeoutException) as exc_info:
            await flat_manager_client.create_build("stable", "https://build.log/url")

        assert "Request timed out" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_build_network_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "request", side_effect=NetworkError("Network unreachable")
        )
        with pytest.raises(NetworkError) as exc_info:
            await flat_manager_client.create_build("stable", "https://build.log/url")

        assert "Network unreachable" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_build_unexpected_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("request", side_effect=ValueError("Unexpected error"))
        with pytest.raises(ValueError) as exc_info:
            await flat_manager_client.create_build("stable", "https://build.log/url")

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
        mock_httpx.set_response("request")
        await flat_manager_client.commit("12345")
        mock_httpx.request.assert_called_once_with(
            "POST",
            "https://test.flathub.org/api/v1/build/12345/commit",
            headers={"Authorization": "Bearer test_token"},
            timeout=30.0,
            json={
                "endoflife": None,
                "endoflife_rebase": None,
            },
        )

    @pytest.mark.asyncio
    async def test_commit_with_end_of_life(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("request")
        await flat_manager_client.commit(
            "12345",
            end_of_life="This application has been replaced by org.new.app.",
            end_of_life_rebase="org.new.app",
        )
        mock_httpx.request.assert_called_once_with(
            "POST",
            "https://test.flathub.org/api/v1/build/12345/commit",
            headers={"Authorization": "Bearer test_token"},
            timeout=30.0,
            json={
                "endoflife": "This application has been replaced by org.new.app.",
                "endoflife_rebase": "org.new.app",
            },
        )

    @pytest.mark.asyncio
    async def test_commit_http_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "request",
            raise_for_status=HTTPStatusError(
                "Bad Request", request=MagicMock(), response=MagicMock()
            ),
        )
        with pytest.raises(HTTPStatusError):
            await flat_manager_client.commit("12345")


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("request")
        await flat_manager_client.publish("12345")
        mock_httpx.request.assert_called_once_with(
            "POST",
            "https://test.flathub.org/api/v1/build/12345/publish",
            headers={"Authorization": "Bearer test_token"},
            timeout=30.0,
            json={},
        )

    @pytest.mark.asyncio
    async def test_publish_http_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "request",
            raise_for_status=HTTPStatusError(
                "Not Found", request=MagicMock(), response=MagicMock()
            ),
        )
        with pytest.raises(HTTPStatusError):
            await flat_manager_client.publish("12345")


class TestPurge:
    @pytest.mark.asyncio
    async def test_purge_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("request")
        await flat_manager_client.purge("12345")
        mock_httpx.request.assert_called_once_with(
            "POST",
            "https://test.flathub.org/api/v1/build/12345/purge",
            headers={"Authorization": "Bearer test_token"},
            timeout=30.0,
            json={},
        )

    @pytest.mark.asyncio
    async def test_purge_http_error(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "request",
            raise_for_status=HTTPStatusError(
                "Forbidden", request=MagicMock(), response=MagicMock()
            ),
        )
        with pytest.raises(HTTPStatusError):
            await flat_manager_client.purge("12345")


class TestRepublish:
    @pytest.mark.asyncio
    async def test_republish_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response("request", json_data={"status": "ok"})
        result = await flat_manager_client.republish(
            "stable",
            "org.test.App",
            end_of_life="This application has been replaced by org.test.NewApp.",
            end_of_life_rebase="org.test.NewApp",
        )

        assert result == {"status": "ok"}

        mock_httpx.request.assert_called_once_with(
            "POST",
            "https://test.flathub.org/api/v1/repo/stable/republish",
            headers={"Authorization": "Bearer test_token"},
            timeout=30.0,
            json={
                "app": "org.test.App",
                "endoflife": "This application has been replaced by org.test.NewApp.",
                "endoflife_rebase": "org.test.NewApp",
            },
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

        with pytest.raises(HTTPStatusError):
            await flat_manager_client.get_job(999)


class TestMiscellaneous:
    @pytest.mark.asyncio
    async def test_create_token_subset_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "request",
            json_data={
                "token": "new_token_12345",
                "sub": "build/12345",
                "scope": ["upload"],
            },
        )
        result = await flat_manager_client.create_token_subset("12345", "org.test.App")

        assert result == "new_token_12345"

        mock_httpx.request.assert_called_once_with(
            "POST",
            "https://test.flathub.org/api/v1/token_subset",
            headers={"Authorization": "Bearer test_token"},
            timeout=30.0,
            json={
                "name": "upload",
                "sub": "build/12345",
                "scope": ["upload"],
                "prefix": ["org.test.App"],
                "duration": 24 * 60 * 60,
            },
        )

    @pytest.mark.asyncio
    async def test_get_build_info_success(self, flat_manager_client, mock_httpx):
        mock_httpx.set_response(
            "request",
            json_data={
                "id": "12345",
                "repo": "stable",
                "status": "committed",
                "extra_data": {"commit_job_id": 123},
            },
        )

        result = await flat_manager_client.get_build_info("12345")

        assert result["id"] == "12345"
        assert result["repo"] == "stable"
        assert result["status"] == "committed"

        mock_httpx.request.assert_called_once_with(
            "GET",
            "https://test.flathub.org/api/v1/build/12345/extended",
            headers={"Authorization": "Bearer test_token"},
            timeout=30.0,
        )

    def test_get_flatpakref_url(self, flat_manager_client):
        result = flat_manager_client.get_flatpakref_url("12345", "org.test.App")
        assert (
            result == "https://dl.flathub.org/build-repo/12345/org.test.App.flatpakref"
        )
