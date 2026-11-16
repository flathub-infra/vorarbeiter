import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import cast
from urllib.parse import urlsplit

from pydantic_ai.common_tools.web_fetch import WebFetchResult, web_fetch_tool
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import BinaryContent

FLATHUB = "https://docs.flathub.org/docs/for-app-authors"
FLATPAK = "https://docs.flatpak.org/en/latest"
PAGES = {
    "linter": f"{FLATHUB}/linter",
    "requirements": f"{FLATHUB}/requirements",
    "metainfo-guidelines": f"{FLATHUB}/metainfo-guidelines",
    "quality-guidelines": f"{FLATHUB}/metainfo-guidelines/quality-guidelines",
    "maintenance": f"{FLATHUB}/maintenance",
    "runtimes": f"{FLATHUB}/runtimes",
    "shared-modules": f"{FLATHUB}/shared-modules",
    "manifests": f"{FLATPAK}/manifests.html",
    "module-sources": f"{FLATPAK}/module-sources.html",
    "sandbox-permissions": f"{FLATPAK}/sandbox-permissions.html",
    "flatpak-builder-reference": f"{FLATPAK}/flatpak-builder-command-reference.html",
    "flatpak-reference": f"{FLATPAK}/flatpak-command-reference.html",
    "flatpak-manifest-man": "https://man.archlinux.org/man/flatpak-manifest.5.en.txt",
}
URLS = set(PAGES.values())
PATHS = {urlsplit(url).path: url for url in URLS}
HOSTS = sorted({urlsplit(url).netloc for url in URLS})
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
TIMEOUT = 20
MAX_LINE_CHARS = 500
READ_MAX_LINES = 150
FIND_MAX_HITS = 30
CACHE_SIZE = 8
CACHE_TTL = 24 * 3600

ANCHOR = re.compile(r'\[\u200b\]\(#([^\s)]+) "Direct link to [^"]*"\)')
SETEXT_H1 = re.compile(r"=+")
SPHINX_ANCHOR = " 🔗"

_web_fetch = cast(
    Callable[[str], Awaitable[WebFetchResult | BinaryContent]],
    web_fetch_tool(
        max_content_length=None,
        timeout=TIMEOUT,
        max_download_bytes=MAX_DOWNLOAD_BYTES,
        allowed_domains=HOSTS,
    ).function,
)
_cache: OrderedDict[str, tuple[float, list[str], dict[str, int]]] = OrderedDict()


async def _fetch(url: str) -> WebFetchResult | BinaryContent:
    return await _web_fetch(url)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _clean(content: str) -> tuple[list[str], dict[str, int]]:
    lines = content.splitlines()
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if line.startswith("# ")
            or (
                line.strip()
                and i + 1 < len(lines)
                and SETEXT_H1.fullmatch(lines[i + 1])
            )
        ),
        0,
    )
    end = next(
        (
            i
            for i in range(start, len(lines))
            if lines[i].startswith("[Edit this page]") or lines[i] == "On this page"
        ),
        len(lines),
    )
    out: list[str] = []
    anchors: dict[str, int] = {}
    for line in lines[start:end]:
        if anchor := ANCHOR.search(line):
            line = ANCHOR.sub("", line)
            anchors.setdefault(anchor[1].lower(), len(out) + 1)
        elif line.startswith("#") and line.endswith(SPHINX_ANCHOR):
            line = line.removesuffix(SPHINX_ANCHOR)
            anchors.setdefault(_slug(line.lstrip("#")), len(out) + 1)
        out.append(line[:MAX_LINE_CHARS])
    return out, anchors


async def _page(url: str) -> tuple[list[str], dict[str, int]]:
    entry = _cache.pop(url, None)
    if entry is None or entry[0] <= time.monotonic():
        result = await _fetch(url)
        if isinstance(result, BinaryContent):
            raise ModelRetry(f"{url} returned {result.media_type}, not text")
        entry = (time.monotonic() + CACHE_TTL, *_clean(result["content"]))
    _cache[url] = entry
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return entry[1], entry[2]


async def read(page: str, find: str = "", start_line: int = 1) -> str:
    ref, _, fragment = page.strip().partition("#")
    url = PAGES.get(ref) or (ref if ref in URLS else PATHS.get(ref))
    if url is None:
        return f"unknown page {page}; pages: {', '.join(PAGES)}"
    try:
        lines, anchors = await _page(url)
    except ModelRetry as exc:
        return str(exc)
    if find:
        needle = find.casefold()
        hits = [n for n, line in enumerate(lines, 1) if needle in line.casefold()]
        out = [f"{n}: {lines[n - 1]}" for n in hits[:FIND_MAX_HITS]]
        if len(hits) > FIND_MAX_HITS:
            out.append(f"({len(hits) - FIND_MAX_HITS} more hits)")
        return "\n".join(out) or "no hits"
    if fragment:
        if (start := anchors.get(fragment.lower())) is None:
            return f"no heading #{fragment} on {url}"
    else:
        start = max(1, min(start_line, len(lines)))
    end = min(len(lines), start + READ_MAX_LINES - 1)
    numbered = [f"{n}: {lines[n - 1]}" for n in range(start, end + 1)]
    return "\n".join([*numbered, f"(page has {len(lines)} lines)"])
