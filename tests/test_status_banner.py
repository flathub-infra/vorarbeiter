import time

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from app.status_banner import get_status_banner, StatusBannerCache, _CACHE_TTL


def make_response(data: dict, status_code: int = 200):
    mock = MagicMock()
    mock.json.return_value = data
    if status_code >= 400:
        mock.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error",
            request=MagicMock(),
            response=MagicMock(),
        )
    else:
        mock.raise_for_status.return_value = None
    return mock


@pytest.fixture
def cache():
    c = StatusBannerCache()
    c.timestamp = time.monotonic() - _CACHE_TTL - 1
    return c


DISRUPTED_DATA = {
    "summaryStatus": "disrupted",
    "systems": [
        {
            "name": "Main repository server",
            "status": "disrupted",
            "unresolvedIssues": [
                {
                    "title": "Publish outage",
                    "permalink": "https://status.flathub.org/issues/2026-02-17-publish/",
                    "severity": "disrupted",
                    "resolved": False,
                    "informational": False,
                }
            ],
        }
    ],
}

DOWN_DATA = {
    "summaryStatus": "down",
    "systems": [
        {
            "name": "API",
            "status": "down",
            "unresolvedIssues": [
                {
                    "title": "API outage",
                    "permalink": "https://status.flathub.org/issues/2026-01-01-api/",
                    "severity": "down",
                    "resolved": False,
                    "informational": False,
                }
            ],
        }
    ],
}

OK_DATA = {
    "summaryStatus": "ok",
    "systems": [],
}

NOTICE_DATA = {
    "summaryStatus": "notice",
    "systems": [],
}


@pytest.mark.asyncio
async def test_returns_banner_when_disrupted(cache):
    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=make_response(DISRUPTED_DATA)
        )
        result = await get_status_banner(cache)

    assert result is not None
    assert result["severity"] == "error"
    assert result["label"] == "Outage"
    assert result["summary_status"] == "disrupted"
    assert isinstance(result["issues"], list)
    assert len(result["issues"]) == 1
    assert result["issues"][0]["system"] == "Main repository server"
    assert (
        result["issues"][0]["permalink"]
        == "https://status.flathub.org/issues/2026-02-17-publish/"
    )


@pytest.mark.asyncio
async def test_returns_banner_when_down(cache):
    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=make_response(DOWN_DATA)
        )
        result = await get_status_banner(cache)

    assert result is not None
    assert result["severity"] == "error"
    assert result["summary_status"] == "down"
    assert len(result["issues"]) == 1


@pytest.mark.asyncio
async def test_returns_none_when_ok(cache):
    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=make_response(OK_DATA)
        )
        result = await get_status_banner(cache)

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_notice(cache):
    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=make_response(NOTICE_DATA)
        )
        result = await get_status_banner(cache)

    assert result is None


@pytest.mark.asyncio
async def test_skips_resolved_issues(cache):
    data = {
        "summaryStatus": "disrupted",
        "systems": [
            {
                "name": "CDN",
                "status": "disrupted",
                "unresolvedIssues": [
                    {
                        "title": "CDN issue",
                        "permalink": "https://status.flathub.org/issues/cdn/",
                        "severity": "disrupted",
                        "resolved": True,
                        "informational": False,
                    }
                ],
            }
        ],
    }
    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=make_response(data)
        )
        result = await get_status_banner(cache)

    assert result is not None
    assert result["issues"] == []


@pytest.mark.asyncio
async def test_skips_informational_issues(cache):
    data = {
        "summaryStatus": "disrupted",
        "systems": [
            {
                "name": "CDN",
                "status": "disrupted",
                "unresolvedIssues": [
                    {
                        "title": "Maintenance",
                        "permalink": "https://status.flathub.org/issues/maintenance/",
                        "severity": "disrupted",
                        "resolved": False,
                        "informational": True,
                    }
                ],
            }
        ],
    }
    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=make_response(data)
        )
        result = await get_status_banner(cache)

    assert result is not None
    assert result["issues"] == []


@pytest.mark.asyncio
async def test_returns_stale_cache_on_http_error(cache):
    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=httpx.ConnectError("connection failed")
        )
        result = await get_status_banner(cache)

    assert result is None


@pytest.mark.asyncio
async def test_returns_stale_cache_on_timeout(cache):
    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            side_effect=httpx.TimeoutException("timed out")
        )
        result = await get_status_banner(cache)

    assert result is None


@pytest.mark.asyncio
async def test_returns_stale_cache_on_non_200(cache):
    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=make_response({}, status_code=500)
        )
        result = await get_status_banner(cache)

    assert result is None


@pytest.mark.asyncio
async def test_multiple_affected_systems(cache):
    data = {
        "summaryStatus": "disrupted",
        "systems": [
            {
                "name": "CDN",
                "status": "disrupted",
                "unresolvedIssues": [
                    {
                        "title": "CDN issue",
                        "permalink": "https://status.flathub.org/issues/cdn/",
                        "severity": "disrupted",
                        "resolved": False,
                        "informational": False,
                    }
                ],
            },
            {
                "name": "API",
                "status": "disrupted",
                "unresolvedIssues": [
                    {
                        "title": "API issue",
                        "permalink": "https://status.flathub.org/issues/api/",
                        "severity": "disrupted",
                        "resolved": False,
                        "informational": False,
                    }
                ],
            },
        ],
    }
    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=make_response(data)
        )
        result = await get_status_banner(cache)

    assert result is not None
    assert isinstance(result["issues"], list)
    assert len(result["issues"]) == 2
    systems = [i["system"] for i in result["issues"]]
    assert "CDN" in systems
    assert "API" in systems


@pytest.mark.asyncio
async def test_uses_cache_when_valid(cache):
    cached_data = {
        "severity": "error",
        "label": "Outage",
        "summary_status": "disrupted",
        "issues": [],
        "status_url": "https://status.flathub.org",
    }
    cache.set(cached_data, time.monotonic())

    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        result = await get_status_banner(cache)
        mock_client.assert_not_called()

    assert result == cached_data


@pytest.mark.asyncio
async def test_refreshes_cache_when_expired(cache):
    cache.set(None, time.monotonic() - _CACHE_TTL - 1)

    with patch("app.status_banner.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=make_response(DISRUPTED_DATA)
        )
        result = await get_status_banner(cache)
        mock_client.assert_called_once()

    assert result is not None
