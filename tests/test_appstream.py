import gzip

import httpx2 as httpx
import pytest

from app.services import appstream

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clear_cache():
    appstream._cache.clear()
    yield
    appstream._cache.clear()


@pytest.fixture
def upstream(monkeypatch):
    responses = {}
    requests = []
    real_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        response = responses[request.url.path.split("/")[-2]]
        if isinstance(response, Exception):
            raise response
        status, body = response
        return httpx.Response(status, stream=httpx.ByteStream(body))

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(appstream.httpx, "AsyncClient", client)
    return responses, requests


def metadata(body):
    return gzip.compress(f"<components>{body}</components>".encode())


def component(body="", app_id="org.example.App"):
    return f"<component><id>{app_id}</id>{body}</component>"


@pytest.mark.parametrize("first", [None, component(app_id="org.example.Other")])
async def test_fallback_matches_desktop_id(upstream, first):
    responses, requests = upstream
    responses["x86_64"] = (404, b"") if first is None else (200, metadata(first))
    responses["aarch64"] = (
        200,
        metadata(component("<name>Example</name>", "org.example.App.desktop")),
    )
    result = await appstream.fetch_preview(42, "org.example.App")
    assert result["arch"] == "aarch64"
    assert result["app_id"] == "org.example.App.desktop"
    assert [request.url.path.split("/")[-2] for request in requests] == [
        "x86_64",
        "aarch64",
    ]
    assert all(request.url.host == "dl.flathub.org" for request in requests)


async def test_default_locale_markup_and_metadata(upstream):
    responses, _ = upstream
    body = """
      <name xml:lang="fr">Nom</name><name>Example</name>
      <developer xml:lang="fr"><name>Local</name></developer><developer><name>Developer</name></developer>
      <description xml:lang="fr"><p>Non</p></description>
      <description><p>Use <em class="bad">this</em> and <code>that</code> safely.
      <script>alert(1)</script><span xml:lang="fr">ignore</span> after</p></description>
      <url type="homepage">https://example.org/page</url>
      <url type="contact">https://user@example.org/</url>
      <url type="bugtracker">https://example.org/a b</url>
      <branding><color scheme_preference="light">#ffffff</color><color scheme_preference="dark">red;bad</color></branding>
      <icon type="cached" width="64" height="64">example.png</icon>
      <icon type="cached" width="128" height="128">example icon.png</icon>
      <icon type="cached" width="256" height="256">../evil.png</icon>
      <screenshots><screenshot><caption xml:lang="fr">Non</caption><caption>Caption</caption>
      <image type="source">https://example.org/image.png</image></screenshot></screenshots>
      <releases><release type="development"/><release version="1" timestamp="invalid" date="2024-01-01">
      <description><p>Fix <em>this</em> now.</p></description></release></releases>
    """
    responses["x86_64"] = (200, metadata(component(body)))
    result = await appstream.fetch_preview(42, "org.example.App")
    assert result["name"] == "Example"
    assert result["developer"] == "Developer"
    assert "Use <em>this</em> and <code>that</code> safely." in result["description"]
    assert "<script>" not in result["description"]
    assert "&lt;script&gt;" in result["description"]
    assert "ignore" not in result["description"]
    assert " after" in result["description"]
    assert result["homepage_host"] == "example.org"
    assert [link["url"] for link in result["links"]] == ["https://example.org/page"]
    assert (
        "--gallery-light:#ffffff;--gallery-text-light:#15141b;"
        in result["gallery_style"]
    )
    assert "red;bad" not in result["gallery_style"]
    assert result["icon_url"].endswith("/icons/128x128/example%20icon.png")
    assert result["shots"][0]["caption"] == "Caption"
    assert result["shots"][0]["source"] == "https://example.org/image.png"
    assert result["release"]["date"] == "2024-01-01"
    assert "Fix <em>this</em> now." in result["release"]["notes"]


@pytest.mark.parametrize(
    "body",
    [
        b"not gzip",
        gzip.compress(b"<components>"),
        gzip.compress(b"x" * (1024 * 1024 + 1)),
        b"x" * (1024 * 1024 + 1),
        gzip.compress(
            b'<!DOCTYPE components [<!ENTITY x "test">]><components>&x;</components>'
        ),
        gzip.compress(
            '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE components><components/>'.encode(
                "utf-16"
            )
        ),
    ],
)
async def test_invalid_metadata_is_error_without_fallback(upstream, body):
    responses, requests = upstream
    responses["x86_64"] = (200, body)
    with pytest.raises(appstream.PreviewError):
        await appstream.fetch_preview(42, "org.example.App")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "first", [(500, b""), (302, b""), httpx.ConnectError("offline")]
)
async def test_upstream_error_is_not_absence(upstream, first):
    responses, requests = upstream
    responses["x86_64"] = first
    with pytest.raises(appstream.PreviewError):
        await appstream.fetch_preview(42, "org.example.App")
    assert len(requests) == 1


async def test_missing_metadata_and_errors_are_not_cached(upstream):
    responses, requests = upstream
    responses.update(
        x86_64=(404, b""), aarch64=(200, metadata(component(app_id="other")))
    )
    with pytest.raises(appstream.PreviewUnavailable):
        await appstream.fetch_preview(42, "org.example.App")
    responses["x86_64"] = (200, metadata(component()))
    assert (await appstream.fetch_preview(42, "org.example.App"))["arch"] == "x86_64"
    assert len(requests) == 3


async def test_cache_expiry_and_key(upstream, monkeypatch):
    responses, requests = upstream
    clock = [1000.0]
    monkeypatch.setattr(appstream.time, "monotonic", lambda: clock[0])
    responses["x86_64"] = (200, metadata(component() + component(app_id="other")))
    await appstream.fetch_preview(42, "org.example.App")
    clock[0] += 599
    await appstream.fetch_preview(42, "org.example.App")
    await appstream.fetch_preview(42, "other")
    assert len(requests) == 2
    clock[0] += 1
    await appstream.fetch_preview(42, "org.example.App")
    assert len(requests) == 3
