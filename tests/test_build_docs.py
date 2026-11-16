from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import pytest
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import BinaryContent

from app.services import build_docs
from app.services.build_docs import PAGES, read

LINTER = PAGES["linter"]
DOCUSAURUS = """\
Flatpak builder lint | Flathub Documentation

[Skip to main content](#__docusaurus_skipToContent_fallback)

* [For users](/docs/category/for-users)

On this page

Flatpak builder lint
====================

Intro.

### appstream-missing-screenshots[\u200b](#appstream-missing-screenshots "Direct link to appstream-missing-screenshots")

The catalog is missing screenshots.

[Edit this page](https://github.com/flathub/documentation)

* [Matrix](https://matrix.to/#/#flathub:matrix.org)
"""
FURO = """\
[Skip to content](#furo-main-content)

- [Introduction to Flatpak](https://docs.flatpak.org/en/latest/introduction.html)

# Manifests 🔗

Intro.

### D-Bus access 🔗

Filtered.

On this page

- [Basic properties](#basic-properties)
"""


def page(content: str) -> dict[str, str]:
    return {"url": LINTER, "title": "", "content": content}


@pytest.fixture
def fetch():
    build_docs._cache.clear()
    with patch.object(
        build_docs, "_fetch", AsyncMock(return_value=page(DOCUSAURUS))
    ) as mock:
        yield mock
    build_docs._cache.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref",
    [
        "nope",
        "http://docs.flathub.org/docs/for-app-authors/linter",
        "https://user:pw@docs.flathub.org/docs/for-app-authors/linter",
        "https://docs.flathub.org:8443/docs/for-app-authors/linter",
        "https://docs.flathub.org/docs/for-app-authors/linter?x=1",
        "https://docs.flathub.org/docs/for-app-authors/../for-users/linter",
        "/docs/for-app-authors/../for-users/installation",
        "/docs/for-users/installation",
        "https://evil.example/docs/for-app-authors/linter",
        "//evil.example/docs/for-app-authors/linter",
    ],
)
async def test_rejected_pages_never_fetch(fetch, ref):
    assert (await read(ref)).startswith(f"unknown page {ref}; pages: linter,")
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_key_url_and_path_share_cache(fetch):
    results = {
        await read(ref)
        for ref in ("linter", LINTER, urlsplit(LINTER).path, f"{LINTER}#")
    }
    assert len(results) == 1
    fetch.assert_awaited_once_with(LINTER)


@pytest.mark.asyncio
async def test_docusaurus_navigation_and_anchors_removed(fetch):
    assert await read("linter") == (
        "1: Flatpak builder lint\n"
        "2: ====================\n"
        "3: \n"
        "4: Intro.\n"
        "5: \n"
        "6: ### appstream-missing-screenshots\n"
        "7: \n"
        "8: The catalog is missing screenshots.\n"
        "9: \n"
        "(page has 9 lines)"
    )


@pytest.mark.asyncio
async def test_furo_navigation_and_anchors_removed(fetch):
    fetch.return_value = page(FURO)
    assert await read("manifests#d-bus-access") == (
        "5: ### D-Bus access\n6: \n7: Filtered.\n8: \n(page has 8 lines)"
    )


@pytest.mark.asyncio
async def test_fragment(fetch):
    assert (await read("linter#Appstream-Missing-Screenshots")).startswith(
        "6: ### appstream-missing-screenshots\n"
    )
    assert await read("linter#nope") == f"no heading #nope on {LINTER}"


@pytest.mark.asyncio
async def test_read_window_line_limit_and_clamping(fetch):
    fetch.return_value = page("# T\n" + "\n".join("x" * 600 for _ in range(200)))
    out = (await read("linter")).splitlines()
    assert len(out) == 151
    assert out[1] == "2: " + "x" * 500
    assert out[-1] == "(page has 201 lines)"
    assert (await read("linter", start_line=-5)).startswith("1: # T\n")
    assert (await read("linter", start_line=9999)).startswith("201: ")


@pytest.mark.asyncio
async def test_find(fetch):
    fetch.return_value = page("# T\n" + "\n".join(f"Hit {n}" for n in range(40)))
    out = (await read("linter", find="hIT")).splitlines()
    assert out[0] == "2: Hit 0"
    assert out[-1] == "(10 more hits)"
    assert len(out) == 31
    assert await read("linter", find="absent") == "no hits"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "error"),
    [
        (
            ModelRetry(f"Failed to fetch {LINTER}: 503"),
            f"Failed to fetch {LINTER}: 503",
        ),
        (
            BinaryContent(data=b"%PDF", media_type="application/pdf"),
            f"{LINTER} returned application/pdf, not text",
        ),
    ],
)
async def test_failures_are_not_cached(fetch, result, error):
    if isinstance(result, Exception):
        fetch.side_effect = result
    else:
        fetch.return_value = result
    assert await read("linter") == error
    fetch.side_effect = None
    fetch.return_value = page(DOCUSAURUS)
    assert (await read("linter")).startswith("1: Flatpak builder lint")
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_lru_eviction(fetch):
    keys = list(PAGES)[:9]
    for key in keys[:8]:
        await read(key)
    await read(keys[0])
    await read(keys[8])
    assert fetch.await_count == 9
    await read(keys[0])
    assert fetch.await_count == 9
    await read(keys[1])
    assert fetch.await_count == 10


@pytest.mark.asyncio
async def test_ttl_expiry(fetch):
    with patch.object(build_docs.time, "monotonic", return_value=1000.0):
        await read("linter")
    with patch.object(
        build_docs.time, "monotonic", return_value=1000.0 + build_docs.CACHE_TTL - 1
    ):
        await read("linter")
    assert fetch.await_count == 1
    with patch.object(
        build_docs.time, "monotonic", return_value=1000.0 + build_docs.CACHE_TTL
    ):
        await read("linter")
    assert fetch.await_count == 2


def test_mapped_urls_use_https_on_approved_hosts():
    for url in PAGES.values():
        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.netloc in {
            "docs.flathub.org",
            "docs.flatpak.org",
            "man.archlinux.org",
        }
        assert not url.endswith("/")
