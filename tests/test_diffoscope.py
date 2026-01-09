import io
import zipfile
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.diffoscope import (
    DiffoscopeCache,
    DiffoscopeReport,
    fetch_diffoscope_report,
    rewrite_asset_paths,
)


def create_test_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", b'<html><link href="common.css"></html>')
        zf.writestr("common.css", b"body { color: red; }")
        zf.writestr("icon.png", b"\x89PNG\r\n\x1a\n")  # PNG header bytes
    return buffer.getvalue()


class TestDiffoscopeCache:
    def test_get_nonexistent_key(self):
        cache = DiffoscopeCache(max_size=10)
        assert cache.get("nonexistent") is None

    def test_set_and_get(self):
        cache = DiffoscopeCache(max_size=10)
        report = DiffoscopeReport(html=b"html", css=b"css", icon=b"icon")
        cache.set("key1", report)
        assert cache.get("key1") == report

    def test_lru_eviction(self):
        cache = DiffoscopeCache(max_size=2)
        report1 = DiffoscopeReport(html=b"html1", css=b"css1", icon=b"icon1")
        report2 = DiffoscopeReport(html=b"html2", css=b"css2", icon=b"icon2")
        report3 = DiffoscopeReport(html=b"html3", css=b"css3", icon=b"icon3")

        cache.set("key1", report1)
        cache.set("key2", report2)
        cache.set("key3", report3)

        assert cache.get("key1") is None
        assert cache.get("key2") == report2
        assert cache.get("key3") == report3

    def test_access_updates_order(self):
        cache = DiffoscopeCache(max_size=2)
        report1 = DiffoscopeReport(html=b"html1", css=b"css1", icon=b"icon1")
        report2 = DiffoscopeReport(html=b"html2", css=b"css2", icon=b"icon2")
        report3 = DiffoscopeReport(html=b"html3", css=b"css3", icon=b"icon3")

        cache.set("key1", report1)
        cache.set("key2", report2)
        cache.get("key1")
        cache.set("key3", report3)

        assert cache.get("key1") == report1
        assert cache.get("key2") is None
        assert cache.get("key3") == report3

    def test_update_existing_key(self):
        cache = DiffoscopeCache(max_size=2)
        report1 = DiffoscopeReport(html=b"html1", css=b"css1", icon=b"icon1")
        report2 = DiffoscopeReport(html=b"html2", css=b"css2", icon=b"icon2")

        cache.set("key1", report1)
        cache.set("key1", report2)

        assert cache.get("key1") == report2
        assert len(cache._cache) == 1


class TestFetchDiffoscopeReport:
    @pytest.mark.asyncio
    async def test_fetch_success(self):
        zip_content = create_test_zip()

        mock_response = AsyncMock()
        mock_response.content = zip_content
        mock_response.raise_for_status = lambda: None

        with patch("app.services.diffoscope._cache", DiffoscopeCache()):
            with patch("httpx.AsyncClient") as mock_client:
                mock_instance = AsyncMock()
                mock_instance.get.return_value = mock_response
                mock_instance.__aenter__.return_value = mock_instance
                mock_instance.__aexit__.return_value = None
                mock_client.return_value = mock_instance

                result = await fetch_diffoscope_report("https://example.com/test.zip")

                assert result is not None
                assert b"common.css" in result.html
                assert b"color: red" in result.css
                assert result.icon.startswith(b"\x89PNG")

    @pytest.mark.asyncio
    async def test_fetch_uses_cache(self):
        zip_content = create_test_zip()

        mock_response = AsyncMock()
        mock_response.content = zip_content
        mock_response.raise_for_status = lambda: None

        with patch("app.services.diffoscope._cache", DiffoscopeCache()):
            with patch("httpx.AsyncClient") as mock_client:
                mock_instance = AsyncMock()
                mock_instance.get.return_value = mock_response
                mock_instance.__aenter__.return_value = mock_instance
                mock_instance.__aexit__.return_value = None
                mock_client.return_value = mock_instance

                url = "https://example.com/test.zip"
                result1 = await fetch_diffoscope_report(url)
                result2 = await fetch_diffoscope_report(url)

                assert mock_instance.get.call_count == 1
                assert result1 == result2

    @pytest.mark.asyncio
    async def test_fetch_http_error(self):
        with patch("app.services.diffoscope._cache", DiffoscopeCache()):
            with patch("httpx.AsyncClient") as mock_client:
                mock_instance = AsyncMock()
                mock_instance.get.side_effect = httpx.HTTPStatusError(
                    "Not Found",
                    request=AsyncMock(),
                    response=AsyncMock(status_code=404),
                )
                mock_instance.__aenter__.return_value = mock_instance
                mock_instance.__aexit__.return_value = None
                mock_client.return_value = mock_instance

                result = await fetch_diffoscope_report("https://example.com/test.zip")
                assert result is None

    @pytest.mark.asyncio
    async def test_fetch_invalid_zip(self):
        mock_response = AsyncMock()
        mock_response.content = b"not a zip file"
        mock_response.raise_for_status = lambda: None

        with patch("app.services.diffoscope._cache", DiffoscopeCache()):
            with patch("httpx.AsyncClient") as mock_client:
                mock_instance = AsyncMock()
                mock_instance.get.return_value = mock_response
                mock_instance.__aenter__.return_value = mock_instance
                mock_instance.__aexit__.return_value = None
                mock_client.return_value = mock_instance

                result = await fetch_diffoscope_report("https://example.com/test.zip")
                assert result is None


class TestRewriteAssetPaths:
    def test_rewrite_css_path(self):
        html = b'<link href="common.css" rel="stylesheet">'
        result = rewrite_asset_paths(html, "/diffoscope/abc123")
        assert b'href="/diffoscope/abc123/common.css"' in result

    def test_rewrite_icon_path(self):
        html = b'<img src="icon.png" alt="icon">'
        result = rewrite_asset_paths(html, "/diffoscope/abc123")
        assert b'src="/diffoscope/abc123/icon.png"' in result

    def test_rewrite_multiple_paths(self):
        html = b'<link href="common.css"><img src="icon.png">'
        result = rewrite_asset_paths(html, "/diffoscope/abc123")
        assert b'href="/diffoscope/abc123/common.css"' in result
        assert b'src="/diffoscope/abc123/icon.png"' in result
