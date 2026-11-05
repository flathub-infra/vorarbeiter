import asyncio
import base64
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import pytest
from gi.repository import GLib

from app.models import Pipeline
from app.services import build_sizes
from app.services.build_sizes import BuildSize, fetch_build_sizes, parse_build_sizes
from app.services.github_notifier import GitHubNotifier


@pytest.fixture
def exported_summary():
    # Real Flatpak 1.18.4 build-export summary: metadata + a 21-byte payload.
    # OSTree content sizes are 159 bytes compressed and 1024 bytes installed
    # (two files rounded to 512-byte blocks), not summary/archive file sizes.
    return base64.b64decode(
        (Path(__file__).parent / "fixtures/build-summary.base64").read_bytes()
    )


def summary(entries, version=0):
    refs, cache = [], {}
    for ref, sizes in entries:
        metadata = {}
        if sizes is not None:
            installed, download = sizes
            value = (
                int.from_bytes(installed.to_bytes(8, "big"), sys.byteorder),
                int.from_bytes(download.to_bytes(8, "big"), sys.byteorder),
                "[Application]\n",
            )
            cache[ref] = value
            metadata["xa.data"] = GLib.Variant("(tts)", value)
        refs.append((ref, (999999, bytes(32), metadata)))
    metadata = {
        "xa.summary-version": GLib.Variant(
            "u", int.from_bytes(version.to_bytes(4, "little"), sys.byteorder)
        )
    }
    if version == 0:
        metadata["xa.cache"] = GLib.Variant("v", GLib.Variant("a{s(tts)}", cache))
    return (
        GLib.Variant(build_sizes._SUMMARY_TYPE, (refs, metadata))
        .get_data_as_bytes()
        .get_data()
    )


def test_real_export(exported_summary):
    assert parse_build_sizes(exported_summary, "org.test.App") == [
        BuildSize("x86_64", 159, 1024)
    ]
    assert parse_build_sizes(exported_summary, "org.other.App") == []


@pytest.mark.parametrize("version", [0, 1])
def test_architecture_scope_and_byte_order(version):
    data = summary(
        [
            ("app/org.test.App/x86_64/test", (2**32 + 512, 2**30 + 123)),
            ("app/org.test.App/aarch64/test", (2048, 1024)),
            ("runtime/org.test.App.Locale/x86_64/test", (999, 999)),
            ("runtime/org.test.App/x86_64/test", (999, 999)),
            ("app/org.other.App/x86_64/test", (999, 999)),
            ("app/org.test.App/arm/test", None),
        ],
        version,
    )
    assert parse_build_sizes(data, "org.test.App") == [
        BuildSize("aarch64", 1024, 2048),
        BuildSize("x86_64", 2**30 + 123, 2**32 + 512),
    ]


def test_ambiguous_branches_and_unsafe_architecture():
    data = summary(
        [
            ("app/org.test.App/x86_64/test", (123, 456)),
            ("app/org.test.App/x86_64/stable", None),
            ("app/org.test.App/<img>/test", (123, 456)),
            ("app/org.test.App/aarch64/test", (0, 0)),
        ]
    )
    assert parse_build_sizes(data, "org.test.App") == [BuildSize("aarch64", 0, 0)]


@pytest.mark.parametrize("data", [b"", b"invalid", b"\xff" * 128, summary([], 2)])
def test_invalid_or_unsupported_metadata(data):
    assert parse_build_sizes(data, "org.test.App") == []


def test_parser_bounds(exported_summary, monkeypatch):
    monkeypatch.setattr(build_sizes, "_MAX_SUMMARY", len(exported_summary) - 1)
    assert parse_build_sizes(exported_summary, "org.test.App") == []
    monkeypatch.setattr(build_sizes, "_MAX_SUMMARY", 2 * 1024 * 1024)
    monkeypatch.setattr(build_sizes, "_MAX_REFS", 0)
    assert parse_build_sizes(exported_summary, "org.test.App") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, "404", "redirect", "encoding", "large", "timeout", "malformed"]
)
async def test_fetch_bounded_metadata(exported_summary, monkeypatch, failure):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout")
        status = {"404": 404, "redirect": 302}.get(failure, 200)
        headers = {"Content-Encoding": "gzip"} if failure == "encoding" else {}
        content = b"invalid" if failure == "malformed" else exported_summary
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))

    if failure == "large":
        monkeypatch.setattr(build_sizes, "_MAX_SUMMARY", 50)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.object(build_sizes.httpx, "AsyncClient", return_value=client):
        result = await fetch_build_sizes(123, "org.test.App")
    assert result == ([] if failure else [BuildSize("x86_64", 159, 1024)])
    assert len(requests) == 1
    assert str(requests[0].url) == "https://dl.flathub.org/build-repo/123/summary"
    assert requests[0].headers["Accept-Encoding"] == "identity"


@pytest.mark.asyncio
async def test_total_deadline_and_cancellation(monkeypatch):
    closed = []

    class StalledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.append(True)

    def client():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=StalledStream())
            )
        )

    monkeypatch.setattr(build_sizes, "_TIMEOUT", 0.01)
    first_client = client()
    with patch.object(build_sizes.httpx, "AsyncClient", return_value=first_client):
        assert await fetch_build_sizes(123, "org.test.App") == []
    assert closed == [True]

    monkeypatch.setattr(build_sizes, "_TIMEOUT", 10)
    second_client = client()
    with (
        patch.object(build_sizes.httpx, "AsyncClient", return_value=second_client),
        pytest.raises(TimeoutError),
    ):
        async with asyncio.timeout(0.01):
            await fetch_build_sizes(123, "org.test.App")
    assert closed == [True, True]


@pytest.mark.asyncio
async def test_comment_integration_and_unavailability(exported_summary):
    pipeline = Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        build_id=123,
        params={"repo": "flathub/flathub", "pr_number": "42"},
        log_url=None,
        flat_manager_repo="test",
    )
    sizes = parse_build_sizes(exported_summary, pipeline.app_id)
    with (
        patch(
            "app.services.github_notifier.fetch_build_sizes", new_callable=AsyncMock
        ) as fetch,
        patch(
            "app.services.github_notifier.create_pr_comment", new_callable=AsyncMock
        ) as post,
    ):
        fetch.return_value = []
        await GitHubNotifier().notify_pr_build_complete(pipeline, "committed")
        original = post.call_args.kwargs["comment"]
        assert "Build details" not in original
        fetch.return_value = sizes
        await GitHubNotifier().notify_pr_build_complete(pipeline, "committed")
        enriched = post.call_args.kwargs["comment"]
        assert enriched.startswith(original)
        assert "<details><summary>Build details</summary>" in enriched
        assert "| x86_64 | 159 B | 1.0 KiB |" in enriched
        assert "excludes runtimes and extensions" in enriched
        assert "Preview app listing" in enriched
        fetch.reset_mock()
        await GitHubNotifier().notify_pr_build_complete(pipeline, "cancelled")
        fetch.assert_not_awaited()


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (2**20, "1.0 MiB"),
        (2**30, "1.0 GiB"),
    ],
)
def test_binary_units(value, expected):
    assert build_sizes._format_bytes(value) == expected


def test_comment_size_bounds():
    data = summary([(f"app/org.test.App/{'a' * 33}/test", (1, 1))])
    assert parse_build_sizes(data, "org.test.App") == []
    data = summary([(f"app/org.test.App/arch{i}/test", (1, 1)) for i in range(33)])
    assert parse_build_sizes(data, "org.test.App") == []
