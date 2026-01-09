import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO

import httpx
import structlog

logger = structlog.get_logger(__name__)

MAX_CACHE_SIZE = 100


@dataclass
class DiffoscopeReport:
    html: bytes
    css: bytes
    icon: bytes


class DiffoscopeCache:
    """LRU cache for diffoscope report contents."""

    def __init__(self, max_size: int = MAX_CACHE_SIZE):
        self._cache: OrderedDict[str, DiffoscopeReport] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str) -> DiffoscopeReport | None:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def set(self, key: str, report: DiffoscopeReport) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        elif len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[key] = report


_cache = DiffoscopeCache()


async def fetch_diffoscope_report(result_url: str) -> DiffoscopeReport | None:
    """Fetch and extract diffoscope report from zip URL."""
    cached = _cache.get(result_url)
    if cached:
        logger.debug("Diffoscope report cache hit", url=result_url)
        return cached

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(result_url)
            response.raise_for_status()

            zip_content = BytesIO(response.content)

            with zipfile.ZipFile(zip_content, "r") as zf:
                namelist = zf.namelist()
                html = zf.read("index.html") if "index.html" in namelist else b""
                css = zf.read("common.css") if "common.css" in namelist else b""
                icon = zf.read("icon.png") if "icon.png" in namelist else b""

            report = DiffoscopeReport(html=html, css=css, icon=icon)
            _cache.set(result_url, report)
            logger.info("Fetched and cached diffoscope report", url=result_url)
            return report

    except httpx.HTTPStatusError as e:
        logger.error(
            "HTTP error fetching diffoscope report",
            url=result_url,
            status_code=e.response.status_code,
        )
        return None
    except zipfile.BadZipFile:
        logger.error("Invalid zip file for diffoscope report", url=result_url)
        return None
    except Exception as e:
        logger.error("Failed to fetch diffoscope report", url=result_url, error=str(e))
        return None


def rewrite_asset_paths(html: bytes, base_path: str) -> bytes:
    """Rewrite relative asset paths in HTML to use our serving endpoints."""
    html_str = html.decode("utf-8")
    html_str = html_str.replace('href="common.css"', f'href="{base_path}/common.css"')
    html_str = html_str.replace('src="icon.png"', f'src="{base_path}/icon.png"')
    return html_str.encode("utf-8")
