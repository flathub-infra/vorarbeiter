import hashlib
import io
import json
import stat
import tarfile
import zipfile
from typing import Literal

import pytest
import zstandard

from app.config import settings
from app.services import build_sources
from app.services.build_sources import SourceWorkspace, _SourceError


class FakeRepo:
    def __init__(self, files=None):
        self.files = files or {}
        self.calls = []

    async def __call__(self, path):
        self.calls.append(path)
        return self.files.get(path)


def manifest(*modules):
    return json.dumps({"app-id": "org.test.App", "modules": list(modules)})


def module(name, sources):
    return {"name": name, "sources": sources}


def digest(data, algorithm="sha256"):
    return hashlib.new(algorithm, data).hexdigest()


def tar_bytes(
    files=None,
    symlinks=None,
    hardlinks=None,
    fifo=None,
    device=None,
    mode: Literal["w", "w:gz", "w:bz2", "w:xz"] = "w:gz",
):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, data in (files or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
        for name, target in (hardlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.LNKTYPE
            info.linkname = target
            archive.addfile(info)
        for name in fifo or []:
            info = tarfile.TarInfo(name)
            info.type = tarfile.FIFOTYPE
            archive.addfile(info)
        for name in device or []:
            info = tarfile.TarInfo(name)
            info.type = tarfile.CHRTYPE
            info.devmajor = 1
            info.devminor = 3
            archive.addfile(info)
    return buffer.getvalue()


def zip_bytes(files=None, symlinks=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in (files or {}).items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return buffer.getvalue()


@pytest.fixture
def configured(monkeypatch, tmp_path):
    values = {
        "source_download_max_bytes": 1024 * 1024,
        "source_extracted_max_bytes": 1024 * 1024,
        "source_max_files": 100,
        "source_timeout_seconds": 5,
        "source_cache_entries": 8,
        "source_read_max_bytes": 1024 * 1024,
        "source_search_max_bytes": 1024 * 1024,
        "source_search_max_files": 100,
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(build_sources, "_cache_root", lambda: tmp_path / "cache")
    return values


@pytest.fixture
def workspace_factory(configured):
    workspaces = []

    async def make(
        manifest_data, repo_files=None, path="org.test.App.json", repo_bytes=None
    ):
        repo = FakeRepo(repo_files)
        text = (
            manifest_data
            if isinstance(manifest_data, str)
            else json.dumps(manifest_data)
        )
        workspace = await SourceWorkspace.create(
            path, text, repo, repo_bytes=repo_bytes
        )
        workspaces.append(workspace)
        return workspace, repo

    yield make
    for workspace in workspaces:
        workspace.close()


def mock_download(monkeypatch, payloads=None, failures=None):
    payloads = payloads or {}
    failures = failures or {}
    calls = []

    def fake(url, max_bytes, timeout):
        calls.append(url)
        if url in failures:
            raise _SourceError(str(failures[url]))
        if url not in payloads:
            raise _SourceError(f"unexpected URL {url}")
        return payloads[url]

    monkeypatch.setattr(build_sources, "_download_once", fake)
    return calls


@pytest.mark.asyncio
async def test_archive_read_search_and_provenance(workspace_factory, monkeypatch):
    payload = tar_bytes(
        files={
            "app-1.0/hello.c": b"int main(void) {\n  return 0;\n}\n",
            "app-1.0/README": b"readme\n",
        }
    )
    url = "https://example.com/app.tar.gz"
    mock_download(monkeypatch, {url: payload})
    data = manifest(
        module(
            "app",
            [
                {
                    "type": "archive",
                    "url": url,
                    "sha256": digest(payload),
                    "strip-components": 1,
                    "dest": "src",
                },
                {
                    "type": "inline",
                    "contents": "generated\n",
                    "dest-filename": "generated.txt",
                },
                {
                    "type": "patch",
                    "contents": "--- a\n+++ b\n",
                    "dest-filename": "fix.patch",
                },
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    report = workspace.report()
    assert "extracted 2 files" in report
    assert "[ok]" in report

    text = workspace.read(
        "app", "src/hello.c", kind="upstream", start_line=2, end_line=2
    )
    assert f"source: archive {url} sha256={digest(payload)}" in text
    assert "2:   return 0;" in text
    assert "(file has 3 lines)" in text

    generated = workspace.read("app", "generated.txt", kind="generated")
    assert "1: generated" in generated

    patch = workspace.read("app", "fix.patch", kind="patch")
    assert "1: --- a" in patch
    assert "2: +++ b" in patch
    assert "unapplied" in report

    hits = workspace.search("RETURN 0", module="app", kind="upstream")
    assert "path=src/hello.c:2:" in hits
    assert url in hits
    assert "hello.c" in hits
    assert "README" not in hits


@pytest.mark.asyncio
async def test_zip_extraction_strip_and_dest(workspace_factory, monkeypatch):
    payload = zip_bytes(
        {
            "pkg/docs/readme.txt": b"zip line one\nzip line two\n",
            "pkg/data.txt": b"data\n",
        }
    )
    url = "https://example.com/pkg.zip"
    mock_download(monkeypatch, {url: payload})
    data = manifest(
        module(
            "zipped",
            [
                {
                    "type": "archive",
                    "url": url,
                    "sha256": digest(payload),
                    "strip-components": 1,
                    "dest": "unpacked",
                    "dest-filename": "ignored.zip",
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    text = workspace.read("zipped", "unpacked/docs/readme.txt")
    assert "1: zip line one" in text
    assert "2: zip line two" in text
    report = workspace.report()
    assert "dest-filename is not applied to archive sources" in report


@pytest.mark.parametrize("archive_type", ["tar", "zip"])
@pytest.mark.asyncio
async def test_archive_default_strip_and_internal_symlink(
    workspace_factory, monkeypatch, archive_type
):
    files = {"pkg/target.txt": b"target\n"}
    symlinks = {"pkg/dir/link": "../target.txt"}
    payload = (
        tar_bytes(files=files, symlinks=symlinks)
        if archive_type == "tar"
        else zip_bytes(files=files, symlinks=symlinks)
    )
    url = f"https://example.com/source.{archive_type}"
    mock_download(monkeypatch, {url: payload})
    data = manifest(
        module(
            "app",
            [{"type": "archive", "url": url, "sha256": digest(payload)}],
        )
    )

    workspace, _ = await workspace_factory(data)
    root = workspace._module_roots["app"]
    assert (root / "target.txt").read_text() == "target\n"
    link = root / "dir/link"
    assert link.is_symlink()
    assert link.read_text() == "target\n"


@pytest.mark.parametrize("archive_type", ["tar", "zip"])
@pytest.mark.asyncio
async def test_stripped_entries_count_toward_archive_limit(
    workspace_factory, monkeypatch, archive_type
):
    files = {"first": b"one\n", "second": b"two\n"}
    payload = tar_bytes(files=files) if archive_type == "tar" else zip_bytes(files)
    url = f"https://example.com/limited.{archive_type}"
    mock_download(monkeypatch, {url: payload})
    monkeypatch.setattr(settings, "source_max_files", 2)
    data = manifest(
        module(
            "limited",
            [{"type": "archive", "url": url, "sha256": digest(payload)}],
        )
    )

    workspace, _ = await workspace_factory(data)
    assert "archive file count exceeds the configured limit" in workspace.report()


@pytest.mark.parametrize("zip64", [False, True])
@pytest.mark.asyncio
async def test_zip_entry_limit_precedes_parser(workspace_factory, monkeypatch, zip64):
    end = bytearray(22)
    end[:4] = b"PK\x05\x06"
    end[10:12] = (0xFFFF if zip64 else 1000).to_bytes(2, "little")
    payload = bytes(end)
    if zip64:
        record = bytearray(56)
        record[:4] = b"PK\x06\x06"
        record[32:40] = (1000).to_bytes(8, "little")
        locator = bytearray(20)
        locator[:4] = b"PK\x06\x07"
        payload = bytes(record + locator + end)
    url = "https://example.com/huge.zip"
    mock_download(monkeypatch, {url: payload})
    monkeypatch.setattr(settings, "source_max_files", 2)

    def fail_if_parsed(*args, **kwargs):
        raise AssertionError("ZipFile was constructed before the entry check")

    monkeypatch.setattr(build_sources.zipfile, "ZipFile", fail_if_parsed)
    data = manifest(
        module(
            "huge",
            [{"type": "zip", "url": url, "sha256": digest(payload)}],
        )
    )

    workspace, _ = await workspace_factory(data)
    assert "archive file count exceeds the configured limit" in workspace.report()


@pytest.mark.asyncio
async def test_hash_mismatch_and_missing_digest_are_explicit(
    workspace_factory, monkeypatch
):
    url = "https://example.com/data.txt"
    mock_download(monkeypatch, {url: b"wrong bytes"})
    data = manifest(
        module(
            "bad",
            [
                {
                    "type": "file",
                    "url": url,
                    "sha256": "0" * 64,
                    "dest-filename": "data.txt",
                },
                {
                    "type": "archive",
                    "url": "https://example.com/no-digest.tar.gz",
                },
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    report = workspace.report()
    assert "sha256 digest mismatch" in report
    assert "has no digest" in report
    assert workspace.read("bad", "data.txt").startswith("data.txt is not materialized")


@pytest.mark.asyncio
async def test_cache_reuse_verifies_digest(workspace_factory, monkeypatch, tmp_path):
    payload = b"good bytes\n"
    url = "https://example.com/cached.txt"
    expected = digest(payload)
    key = build_sources._cache_key([("sha256", expected)], url)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / key).write_bytes(b"poisoned bytes\n")
    (cache / f"{key}.sha256").write_text(digest(b"poisoned bytes\n"), encoding="ascii")
    calls = mock_download(monkeypatch, {url: payload})
    data = manifest(
        module(
            "cached",
            [
                {
                    "type": "file",
                    "url": url,
                    "sha256": expected,
                    "dest-filename": "cached.txt",
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    assert calls == [url]
    text = workspace.read("cached", "cached.txt")
    assert "1: good bytes" in text
    assert (cache / key).read_bytes() == payload


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            tar_bytes(files={"safe.txt": b"safe\n", "../escape.txt": b"evil\n"}),
            "path escapes its root",
        ),
        (
            tar_bytes(files={"safe.txt": b"safe\n", "/etc/passwd": b"evil\n"}),
            "absolute path",
        ),
        (
            tar_bytes(
                files={"pkg/safe.txt": b"safe\n"},
                symlinks={"pkg/link": "/etc/passwd"},
            ),
            "unsafe archive link target",
        ),
        (
            tar_bytes(
                files={"pkg/safe.txt": b"safe\n"},
                hardlinks={"pkg/hard": "../../etc/passwd"},
            ),
            "path escapes its root",
        ),
        (
            tar_bytes(files={"pkg/safe.txt": b"safe\n"}, fifo=["pkg/pipe"]),
            "unsafe archive member type",
        ),
        (
            tar_bytes(files={"pkg/safe.txt": b"safe\n"}, device=["pkg/dev"]),
            "unsafe archive member type",
        ),
        (
            zip_bytes(files={"safe.txt": b"safe\n", "../escape.txt": b"evil\n"}),
            "path escapes its root",
        ),
        (
            zip_bytes(
                files={"pkg/safe.txt": b"safe\n"},
                symlinks={"pkg/link": "/etc/passwd"},
            ),
            "unsafe archive link target",
        ),
    ],
    ids=[
        "tar-traversal",
        "tar-absolute",
        "tar-symlink",
        "tar-hardlink",
        "tar-fifo",
        "tar-device",
        "zip-traversal",
        "zip-symlink",
    ],
)
@pytest.mark.asyncio
async def test_archive_rejects_unsafe_members(
    workspace_factory, monkeypatch, payload, message
):
    url = "https://example.com/unsafe.tar.gz"
    mock_download(monkeypatch, {url: payload})
    data = manifest(
        module(
            "unsafe",
            [
                {
                    "type": "archive",
                    "url": url,
                    "sha256": digest(payload),
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    report = workspace.report()
    assert message in report
    assert workspace.read("unsafe", "safe.txt").startswith(
        "safe.txt is not materialized"
    )


@pytest.mark.asyncio
async def test_shell_and_script_sources_are_not_executed(workspace_factory, tmp_path):
    marker = tmp_path / "executed"
    data = manifest(
        module(
            "commands",
            [
                {
                    "type": "shell",
                    "commands": [f"touch {marker}"],
                },
                {
                    "type": "script",
                    "commands": [f"touch {marker}"],
                },
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    report = workspace.report()
    assert not marker.exists()
    assert "shell source is never executed" in report
    assert "script source is never executed" in report
    assert "not materialized" in report


@pytest.mark.asyncio
async def test_patch_paths_are_stored_unapplied(workspace_factory):
    data = manifest(
        module(
            "patched",
            [{"type": "patch", "paths": ["patches/a.patch", "patches/b.patch"]}],
        )
    )
    workspace, _ = await workspace_factory(
        data,
        {"patches/a.patch": "a change\n", "patches/b.patch": "b change\n"},
    )

    assert "1: a change" in workspace.read("patched", "a.patch", kind="patch")
    assert "1: b change" in workspace.read("patched", "b.patch", kind="patch")
    assert "unapplied" in workspace.report()


@pytest.mark.asyncio
async def test_repository_binary_file_is_preserved(workspace_factory):
    payload = b"\x89PNG\r\n\x1a\n\x00\xff\x00"
    data = manifest(module("icons", [{"type": "file", "path": "assets/icon.png"}]))

    async def repo_bytes(path):
        return payload if path == "assets/icon.png" else None

    workspace, _ = await workspace_factory(data, repo_bytes=repo_bytes)
    root = workspace._module_roots["icons"]
    assert (root / "icon.png").read_bytes() == payload
    assert "binary file" in workspace.read("icons", "icon.png", kind="repository")


@pytest.mark.asyncio
async def test_include_expansion_and_repository_files(workspace_factory):
    repo_files = {
        "modules/app.json": json.dumps(
            module(
                "included",
                [
                    {
                        "type": "inline",
                        "contents": "nested\n",
                        "dest-filename": "nested.txt",
                    }
                ],
            )
        ),
        "fragments/sources.yaml": (
            "- type: inline\n"
            '  contents: "from yaml\\n"\n'
            "  dest-filename: from-yaml.txt\n"
        ),
    }
    data = manifest(
        "modules/app.json",
        {
            "name": "fragment",
            "sources": ["fragments/sources.yaml"],
        },
    )

    workspace, repo = await workspace_factory(data, repo_files)
    assert repo.calls == ["modules/app.json", "fragments/sources.yaml"]
    assert "1: nested" in workspace.read("included", "nested.txt", kind="generated")
    assert "1: from yaml" in workspace.read(
        "fragment", "from-yaml.txt", kind="generated"
    )
    include = workspace.read(
        "manifest", "modules/app.json", kind="repository", end_line=1
    )
    assert "source: repository:modules/app.json" in include
    assert '"included"' in include


@pytest.mark.asyncio
async def test_include_errors_are_reported(workspace_factory):
    repo_files = {
        "modules/a.json": json.dumps({"name": "a", "modules": ["b.json"]}),
        "modules/b.json": json.dumps({"name": "b", "modules": ["a.json"]}),
        "modules/bad.json": "{not json",
    }
    data = manifest(
        "modules/missing.json",
        "modules/a.json",
        "modules/bad.json",
    )

    workspace, _ = await workspace_factory(data, repo_files)
    report = workspace.report()
    assert "repository file not found: modules/missing.json" in report
    assert "include cycle" in report
    assert "could not parse modules/bad.json" in report
    assert "modules/a.json" in report
    assert "modules/b.json" in report


@pytest.mark.asyncio
async def test_source_errors_are_explicit(workspace_factory):
    data = manifest(
        module(
            "errors",
            [
                {"type": "svn", "url": "https://example.com/repo"},
                {"type": "mystery"},
                {"type": "file", "url": "https://example.com/no-digest"},
                {
                    "type": "7z",
                    "url": "https://example.com/archive.7z",
                    "sha256": "0" * 64,
                },
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    report = workspace.report()
    assert "svn sources are not materialized" in report
    assert "unsupported source type: mystery" in report
    assert "has no digest" in report
    assert "unsupported archive type: 7z" in report
    assert "4 sources" in report
    assert "3 errors" in report


@pytest.mark.asyncio
async def test_generic_archive_rejects_known_unsupported_magic(
    workspace_factory, monkeypatch
):
    payload = b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32
    url = "https://example.com/archive.bin"
    mock_download(monkeypatch, {url: payload})
    data = manifest(
        module(
            "magic",
            [
                {
                    "type": "archive",
                    "url": url,
                    "sha256": digest(payload),
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    assert "unsupported archive type: 7z" in workspace.report()


@pytest.mark.asyncio
async def test_git_pinned_archive_submodules_and_lfs(workspace_factory, monkeypatch):
    commit = "a" * 40
    payload = tar_bytes(
        files={
            f"repo-{commit}/main.c": b"int main;\n",
            f"repo-{commit}/.gitmodules": b'[submodule "dep"]\n',
            f"repo-{commit}/asset.bin": (
                b"version https://git-lfs.github.com/spec/v1\n"
                b"oid sha256:" + b"0" * 64 + b"\nsize 10\n"
            ),
        }
    )
    archive_url = f"https://codeload.github.com/o/r/tar.gz/{commit}"
    calls = mock_download(monkeypatch, {archive_url: payload})
    data = manifest(
        module(
            "git",
            [
                {
                    "type": "git",
                    "url": "https://github.com/o/r.git",
                    "commit": commit,
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    assert calls == [archive_url]
    report = workspace.report()
    assert f"pinned commit {commit}" in report
    assert "0 ok, 0 not materialized, 1 errors" in report
    assert "submodule content is not materialized" in report
    assert "git LFS content is not materialized" in report
    assert "1: int main;" in workspace.read("git", "main.c")
    pointer = workspace.read("git", "asset.bin")
    assert "git LFS pointer, content not materialized" in pointer


@pytest.mark.asyncio
async def test_git_requires_full_commit_and_supported_hosts(workspace_factory):
    data = manifest(
        module(
            "tagged",
            [
                {
                    "type": "git",
                    "url": "https://github.com/o/r.git",
                    "tag": "v1.0",
                }
            ],
        ),
        module(
            "short",
            [
                {
                    "type": "git",
                    "url": "https://github.com/o/r.git",
                    "commit": "abc",
                }
            ],
        ),
        module(
            "host",
            [
                {
                    "type": "git",
                    "url": "https://example.com/o/r.git",
                    "commit": "b" * 40,
                }
            ],
        ),
    )

    workspace, _ = await workspace_factory(data)
    report = workspace.report()
    assert "tags and branches are not resolved" in report
    assert "full 40-hex commit" in report
    assert "git host example.com has no supported pinned archive URL" in report


def test_forge_archive_urls():
    commit = "c" * 40
    assert (
        build_sources._forge_archive_url("github.com", ["owner", "repo"], commit)
        == f"https://codeload.github.com/owner/repo/tar.gz/{commit}"
    )
    assert build_sources._forge_archive_url(
        "gitlab.gnome.org", ["GNOME", "app"], commit
    ) == (f"https://gitlab.gnome.org/GNOME/app/-/archive/{commit}/app-{commit}.tar.gz")
    assert (
        build_sources._forge_archive_url("codeberg.org", ["owner", "repo"], commit)
        == f"https://codeberg.org/owner/repo/archive/{commit}.tar.gz"
    )
    with pytest.raises(_SourceError):
        build_sources._forge_archive_url(
            "gitlab.example.com", ["owner", "repo"], commit
        )


@pytest.mark.parametrize(
    ("source_type", "mode"),
    [
        ("tar", "w"),
        ("tar.gz", "w:gz"),
        ("tar.bz2", "w:bz2"),
        ("tar.xz", "w:xz"),
    ],
)
@pytest.mark.asyncio
async def test_tar_variants(workspace_factory, monkeypatch, source_type, mode):
    payload = tar_bytes(files={"pkg/value.txt": b"value\n"}, mode=mode)
    url = f"https://example.com/value.{source_type}"
    mock_download(monkeypatch, {url: payload})
    data = manifest(
        module(
            "variant",
            [
                {
                    "type": source_type,
                    "url": url,
                    "sha256": digest(payload),
                    "strip-components": 1,
                    "dest": "src",
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    assert "1: value" in workspace.read("variant", "src/value.txt")


@pytest.mark.asyncio
async def test_tar_zst_variant(workspace_factory, monkeypatch):
    plain = tar_bytes(files={"pkg/zstd.txt": b"zstd value\n"}, mode="w")
    payload = zstandard.ZstdCompressor().compress(plain)
    url = "https://example.com/value.tar.zst"
    mock_download(monkeypatch, {url: payload})
    data = manifest(
        module(
            "zstd",
            [
                {
                    "type": "tar.zst",
                    "url": url,
                    "sha256": digest(payload),
                    "strip-components": 1,
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    assert "1: zstd value" in workspace.read("zstd", "zstd.txt")


@pytest.mark.asyncio
async def test_mirror_urls_are_tried_after_primary(workspace_factory, monkeypatch):
    payload = b"mirror value\n"
    primary = "https://primary.example/value.txt"
    mirror = "https://mirror.example/value.txt"
    calls = mock_download(
        monkeypatch,
        {mirror: payload},
        {primary: "primary failed"},
    )
    data = manifest(
        module(
            "mirror",
            [
                {
                    "type": "file",
                    "url": primary,
                    "mirror-urls": [mirror],
                    "sha256": digest(payload),
                    "dest-filename": "value.txt",
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    assert calls == [primary, mirror]
    report = workspace.report()
    assert primary in report
    assert "primary failed" in report
    assert mirror in report
    assert "1: mirror value" in workspace.read("mirror", "value.txt")


def test_download_url_validation_rejects_unsafe_shapes():
    for url in (
        "file:///etc/passwd",
        "ftp://example.com/file",
        "https://user:pass@example.com/file",
        "https://user@example.com/file",
        "https:///file",
        "https://example.com:99999/file",
    ):
        with pytest.raises(_SourceError):
            build_sources._validate_download_url(url)


def test_public_ip_rejects_non_global_addresses():
    for host in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "0.0.0.0", "::1"):
        with pytest.raises(_SourceError):
            build_sources._public_ip(host, 443)
    assert build_sources._public_ip("1.1.1.1", 443) == "1.1.1.1"


@pytest.mark.asyncio
async def test_read_search_and_file_limits_are_explicit(workspace_factory, monkeypatch):
    payload = tar_bytes(files={"pkg/line.txt": b"0123456789abcdef\nneedle\n"})
    url = "https://example.com/line.tar.gz"
    mock_download(monkeypatch, {url: payload})
    monkeypatch.setattr(settings, "source_read_max_bytes", 10)
    monkeypatch.setattr(settings, "source_search_max_bytes", 10)
    data = manifest(
        module(
            "limits",
            [
                {
                    "type": "archive",
                    "url": url,
                    "sha256": digest(payload),
                    "strip-components": 1,
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    text = workspace.read("limits", "line.txt")
    assert "read truncated at 10 bytes" in text
    hits = workspace.search("needle", module="limits")
    assert "no hits for 'needle'" in hits
    assert "search stopped at the byte limit" in hits


@pytest.mark.asyncio
async def test_file_count_limit_is_explicit(workspace_factory, monkeypatch):
    payload = tar_bytes(files={"pkg/a.txt": b"a\n", "pkg/b.txt": b"b\n"})
    url = "https://example.com/two.tar.gz"
    mock_download(monkeypatch, {url: payload})
    monkeypatch.setattr(settings, "source_max_files", 2)
    data = manifest(
        module(
            "limit",
            [
                {
                    "type": "archive",
                    "url": url,
                    "sha256": digest(payload),
                    "strip-components": 1,
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    assert "archive file count exceeds the configured limit" in workspace.report()


@pytest.mark.asyncio
async def test_workspace_is_read_only_and_close_removes_it(
    workspace_factory,
):
    data = manifest(
        module(
            "readonly",
            [
                {
                    "type": "inline",
                    "contents": "text\n",
                    "dest-filename": "file.txt",
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    file = next(
        item for item in workspace._file_index.values() if item.module == "readonly"
    )
    assert stat.S_IMODE(file.path.stat().st_mode) & 0o222 == 0
    assert stat.S_IMODE(file.path.parent.stat().st_mode) & 0o222 == 0
    root = workspace._root
    workspace.close()
    assert not root.exists()
    assert workspace.report() == "source workspace is closed"


@pytest.mark.asyncio
async def test_multiple_declared_digests_are_verified(workspace_factory, monkeypatch):
    payload = b"multi digest\n"
    url = "https://example.com/multi.txt"
    mock_download(monkeypatch, {url: payload})
    data = manifest(
        module(
            "digests",
            [
                {
                    "type": "file",
                    "url": url,
                    "sha512": digest(payload, "sha512"),
                    "sha1": digest(payload, "sha1"),
                    "md5": digest(payload, "md5"),
                    "dest-filename": "multi.txt",
                }
            ],
        )
    )

    workspace, _ = await workspace_factory(data)
    assert "1: multi digest" in workspace.read("digests", "multi.txt")
    assert "[ok]" in workspace.report()
