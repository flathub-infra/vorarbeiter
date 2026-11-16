import asyncio
import base64
import binascii
import bz2
import gzip
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import lzma
import os
import posixpath
import re
import shutil
import socket
import ssl
import stat
import tarfile
import tempfile
import time
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import structlog
import yaml

from app.config import settings
from app.utils.manifest import _parse_manifest

logger = structlog.get_logger(__name__)

KINDS = ("upstream", "patch", "generated", "repository")
MAX_READ_LINES = 150
MAX_LINE_CHARS = 500
MAX_SEARCH_HITS = 30
MAX_SEARCH_OUTPUT_CHARS = 24 * 1024
MAX_REPORT_CHARS = 32 * 1024
MAX_REDIRECTS = 5
MAX_DOWNLOAD_URLS = 8
MAX_INCLUDE_DEPTH = 32
MAX_INCLUDES = 200
MAX_SOURCE_ENTRIES = 500
MAX_TREE_ENTRIES = 5000
MAX_IDENTITY_CHARS = 300
MAX_DETAIL_CHARS = 800
MAX_LISTED_PATHS = 20
MAX_LISTED_FILES = 25
USER_AGENT = "vorarbeiter-build-diagnosis"
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"
HEX = re.compile(r"^[0-9a-fA-F]+$")
FULL_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")
SCP_GIT_URL = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[^/:]+):(?P<path>.+)$")
DRIVE_PATH = re.compile(r"^[A-Za-z]:")
DIGEST_SPECS = (
    ("sha512", 128),
    ("sha256", 64),
    ("sha1", 40),
    ("md5", 32),
)
ZIP_TYPES = {"zip", "jar", "apk"}
TAR_TYPES = {
    "tar",
    "tar.gz",
    "tgz",
    "tar.bz2",
    "tbz2",
    "tar.xz",
    "txz",
    "tar.lzma",
    "tlz",
    "tar.zst",
    "tzst",
}
UNSUPPORTED_ARCHIVE_TYPES = {
    "7z",
    "rar",
    "cab",
    "deb",
    "rpm",
    "dmg",
    "iso",
    "tar.z",
    "taz",
}
GITLAB_HOSTS = {
    "invent.kde.org",
    "salsa.debian.org",
    "framagit.org",
    "gitlab.com",
    "gitlab.freedesktop.org",
    "gitlab.gnome.org",
}
STATUS_ORDER = {"error": 0, "skipped": 1, "ok": 2}


class _SourceError(Exception):
    pass


@dataclass
class _Limits:
    download_max_bytes: int
    extracted_max_bytes: int
    max_files: int
    timeout_seconds: float
    cache_entries: int
    read_max_bytes: int
    search_max_bytes: int
    search_max_files: int


@dataclass
class _SourceResult:
    kind: str
    identity: str
    status: str
    detail: str


@dataclass
class _Module:
    name: str
    dirname: str
    root: Path
    results: list[_SourceResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_limit_reported: bool = False


@dataclass
class _File:
    module: str
    module_dir: str
    relpath: str
    path: Path
    kind: str
    source: str
    size: int
    binary: bool


@dataclass
class _Outcome:
    status: str
    detail: str
    notices: list[str] = field(default_factory=list)


@dataclass
class _Download:
    data: bytes
    url: str
    attempts: list[str]
    digests: list[tuple[str, str]]


@dataclass
class _ArchiveStats:
    entries: int = 0
    files: int = 0
    bytes: int = 0


def _limits() -> _Limits:
    return _Limits(
        download_max_bytes=max(1, int(settings.source_download_max_bytes)),
        extracted_max_bytes=max(0, int(settings.source_extracted_max_bytes)),
        max_files=max(0, int(settings.source_max_files)),
        timeout_seconds=max(0.1, float(settings.source_timeout_seconds)),
        cache_entries=max(0, int(settings.source_cache_entries)),
        read_max_bytes=max(1, int(settings.source_read_max_bytes)),
        search_max_bytes=max(1, int(settings.source_search_max_bytes)),
        search_max_files=max(1, int(settings.source_search_max_files)),
    )


def _clean(value: Any) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


def _safe_parts(value: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, str):
        raise _SourceError("path is not text")
    if not value and not allow_empty:
        raise _SourceError("path is empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _SourceError("path contains control characters")
    text = value.replace("\\", "/")
    if text.startswith("/") or DRIVE_PATH.match(text):
        raise _SourceError(f"absolute path is not allowed: {value}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise _SourceError(f"path escapes its root: {value}")
    if not parts and not allow_empty:
        raise _SourceError("path is empty")
    return parts


def _safe_relpath(value: str, allow_empty: bool = False) -> str:
    return "/".join(_safe_parts(value, allow_empty=allow_empty))


def _join_repo_path(base: str, value: str) -> str:
    if not isinstance(value, str):
        raise _SourceError("path is not text")
    if not value:
        raise _SourceError("path is empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _SourceError("path contains control characters")
    text = value.replace("\\", "/")
    if text.startswith("/") or DRIVE_PATH.match(text):
        raise _SourceError(f"absolute path is not allowed: {value}")
    joined = posixpath.join(base, text) if base else text
    normalized = posixpath.normpath(joined)
    if normalized in {"", ".", ".."} or normalized.startswith(("../", "/")):
        raise _SourceError(f"path escapes the repository: {value}")
    return normalized


def _module_dirname(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", _clean(name)).strip("._")
    return (value or "module")[:64]


def _stem(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0] or "module"


def _url_filename(url: str) -> str:
    try:
        path = urlsplit(url).path
    except ValueError:
        path = ""
    name = posixpath.basename(unquote(path).rstrip("/"))
    if not name or name in {".", ".."}:
        return "download"
    return name


def _dest_path(source: dict[str, Any], default_name: str) -> str:
    dest = source.get("dest")
    if dest is not None and not isinstance(dest, str):
        raise _SourceError("dest must be text")
    name = source.get("dest-filename", source.get("filename", default_name))
    if not isinstance(name, str) or not name:
        raise _SourceError("dest-filename must be non-empty text")
    dest_rel = _safe_relpath(dest or "", allow_empty=True)
    name_rel = _safe_relpath(name)
    return f"{dest_rel}/{name_rel}" if dest_rel else name_rel


def _dest_dir(source: dict[str, Any]) -> str:
    dest = source.get("dest")
    if dest is not None and not isinstance(dest, str):
        raise _SourceError("dest must be text")
    return _safe_relpath(dest or "", allow_empty=True)


def _int_field(source: dict[str, Any], name: str, default: int = 0) -> int:
    value = source.get(name, default)
    if value is None:
        return default
    if isinstance(value, bool):
        raise _SourceError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as err:
        raise _SourceError(f"{name} must be an integer") from err
    if result < 0:
        raise _SourceError(f"{name} must not be negative")
    return result


def _hash_data(algorithm: str, data: bytes) -> str:
    try:
        digest = hashlib.new(algorithm, usedforsecurity=False)
    except TypeError:
        digest = hashlib.new(algorithm)
    digest.update(data)
    return digest.hexdigest()


def _declared_digests(source: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for algorithm, length in DIGEST_SPECS:
        value = source.get(algorithm)
        if value is None:
            continue
        if not isinstance(value, str):
            raise _SourceError(f"{algorithm} digest must be text")
        normalized = value.strip().lower()
        if len(normalized) != length or not HEX.fullmatch(normalized):
            raise _SourceError(f"invalid {algorithm} digest")
        result.append((algorithm, normalized))
    return result


def _verify_digests(data: bytes, digests: list[tuple[str, str]]) -> None:
    for algorithm, expected in digests:
        actual = _hash_data(algorithm, data)
        if not hmac.compare_digest(actual, expected):
            raise _SourceError(
                f"{algorithm} digest mismatch: expected {expected}, got {actual}"
            )


def _cache_key(digests: list[tuple[str, str]], url: str) -> str:
    if digests:
        algorithm, value = digests[0]
        return f"{algorithm}-{value}"
    return "url-" + _hash_data("sha256", url.encode())


def _cache_root() -> Path:
    uid = getattr(os, "getuid", lambda: "user")()
    return Path(tempfile.gettempdir()) / f"vorarbeiter-source-cache-{uid}"


class _DownloadCache:
    def __init__(self, entries: int):
        self._entries = entries
        self._root: Path | None = None
        if entries <= 0:
            return
        root = _cache_root()
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if root.is_symlink() or not root.is_dir():
                return
            os.chmod(root, 0o700)
        except OSError:
            return
        self._root = root

    def _paths(self, key: str) -> tuple[Path, Path]:
        if not self._root:
            raise _SourceError("download cache is disabled")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", key):
            raise _SourceError("invalid download cache key")
        path = self._root / key
        return path, path.with_name(key + ".sha256")

    def _delete(self, path: Path, sidecar: Path) -> None:
        with suppress(OSError):
            path.unlink()
        with suppress(OSError):
            sidecar.unlink()

    def get(
        self,
        key: str,
        digests: list[tuple[str, str]],
        max_bytes: int,
    ) -> bytes | None:
        if not self._root:
            return None
        try:
            path, sidecar = self._paths(key)
            if path.is_symlink() or sidecar.is_symlink():
                return None
            if path.stat().st_size > max_bytes:
                self._delete(path, sidecar)
                return None
            data = path.read_bytes()
            expected = sidecar.read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            with suppress(OSError):
                path, sidecar = self._paths(key)
                self._delete(path, sidecar)
            return None
        if not hmac.compare_digest(_hash_data("sha256", data), expected):
            self._delete(path, sidecar)
            return None
        if digests:
            try:
                _verify_digests(data, digests)
            except _SourceError:
                self._delete(path, sidecar)
                return None
        with suppress(OSError):
            os.utime(path, None)
        return data

    def _atomic_write(self, path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            with suppress(OSError):
                os.unlink(temporary)

    def put(self, key: str, data: bytes, digests: list[tuple[str, str]]) -> None:
        if not self._root:
            return
        try:
            path, sidecar = self._paths(key)
            self._atomic_write(path, data)
            self._atomic_write(sidecar, _hash_data("sha256", data).encode("ascii"))
        except OSError:
            with suppress(OSError):
                path, sidecar = self._paths(key)
                self._delete(path, sidecar)
            return
        self._prune()

    def _prune(self) -> None:
        if not self._root:
            return
        try:
            paths = [
                path
                for path in self._root.iterdir()
                if path.is_file()
                and not path.name.startswith(".")
                and not path.name.endswith(".sha256")
            ]
        except OSError:
            return
        excess = len(paths) - self._entries
        if excess <= 0:
            return
        try:
            paths.sort(key=lambda path: path.stat().st_mtime)
        except OSError:
            return
        for path in paths[:excess]:
            sidecar = path.with_name(path.name + ".sha256")
            self._delete(path, sidecar)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        ip: str,
        timeout: float,
        context: ssl.SSLContext,
    ):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip = ip
        self._ssl_context = context

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, self.port), self.timeout)
        try:
            self.sock = self._ssl_context.wrap_socket(raw, server_hostname=self.host)
        except OSError:
            raw.close()
            raise


class _LimitedReader:
    def __init__(self, raw: Any, limit: int):
        self._raw = raw
        self._limit = limit
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = 65536
        data = self._raw.read(size)
        self._position += len(data)
        if self._position > self._limit:
            raise _SourceError("decompressed archive exceeds the configured limit")
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        close = getattr(self._raw, "close", None)
        if close is not None:
            close()


def _validate_download_url(url: str) -> tuple[str, str, int]:
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise _SourceError("URL contains control characters")
    try:
        parts = urlsplit(url)
    except ValueError as err:
        raise _SourceError(f"invalid URL: {url}") from err
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _SourceError(f"unsupported URL scheme: {parts.scheme or '(none)'}")
    if parts.username is not None or parts.password is not None:
        raise _SourceError("URL credentials are not allowed")
    if "\\" in parts.netloc:
        raise _SourceError("URL has an invalid host")
    host = parts.hostname
    if not host:
        raise _SourceError("URL has no host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as err:
        raise _SourceError("URL has an invalid port") from err
    if not 1 <= port <= 65535:
        raise _SourceError("URL has an invalid port")
    return scheme, host.lower(), port


def _public_ip(host: str, port: int) -> str:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as err:
            raise _SourceError(f"could not resolve host {host}: {err}") from err
        raw_addresses = [str(info[4][0]) for info in infos]
    else:
        raw_addresses = [str(literal)]
    addresses: list[str] = []
    for raw in raw_addresses:
        raw = raw.split("%")[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as err:
            raise _SourceError(f"host {host} returned an invalid address") from err
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global:
            raise _SourceError(f"host {host} resolves to non-public address {address}")
        addresses.append(str(address))
    if not addresses:
        raise _SourceError(f"host {host} did not resolve")
    return addresses[0]


def _request_target(url: str) -> str:
    parts = urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    return target


def _set_socket_timeout(
    connection: http.client.HTTPConnection, deadline: float
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _SourceError("download timed out")
    if connection.sock is not None:
        connection.sock.settimeout(remaining)


def _http_get(url: str, max_bytes: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    current = url
    for redirects in range(MAX_REDIRECTS + 1):
        scheme, host, port = _validate_download_url(current)
        ip = _public_ip(host, port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _SourceError("download timed out")
        connection: http.client.HTTPConnection
        if scheme == "https":
            context = ssl.create_default_context()
            connection = _PinnedHTTPSConnection(host, port, ip, remaining, context)
        else:
            connection = _PinnedHTTPConnection(host, port, ip, remaining)
        try:
            try:
                connection.request(
                    "GET",
                    _request_target(current),
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                    },
                )
                _set_socket_timeout(connection, deadline)
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise _SourceError(
                            f"HTTP {response.status} redirect without Location"
                        )
                    if redirects >= MAX_REDIRECTS:
                        raise _SourceError("too many redirects")
                    current = urljoin(current, location)
                    continue
                if response.status != 200:
                    raise _SourceError(f"HTTP {response.status} from {host}")
                length = response.getheader("Content-Length")
                if length:
                    try:
                        announced = int(length)
                    except ValueError:
                        announced = 0
                    if announced > max_bytes:
                        raise _SourceError(f"download exceeds {max_bytes} bytes")
                chunks: list[bytes] = []
                total = 0
                while True:
                    _set_socket_timeout(connection, deadline)
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise _SourceError(f"download exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
            except _SourceError:
                raise
            except (OSError, http.client.HTTPException, ValueError) as err:
                raise _SourceError(f"download from {host} failed: {err}") from err
        finally:
            connection.close()
    raise _SourceError("too many redirects")


def _download_once(url: str, max_bytes: int, timeout: float) -> bytes:
    return _http_get(url, max_bytes, timeout)


def _parse_fragment(path: str, content: str) -> Any:
    suffix = Path(path).suffix.lower()
    try:
        if suffix in {".yaml", ".yml"}:
            return yaml.safe_load(content)
        if suffix == ".json":
            return json.loads(content)
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return yaml.safe_load(content)
    except (ValueError, yaml.YAMLError) as err:
        raise _SourceError(f"could not parse {path}: {err}") from err


def _source_identity(source: dict[str, Any]) -> str:
    kind = _clean(source.get("type") or "source").lower()
    url = source.get("url")
    path = source.get("path")
    commit = source.get("commit")
    if isinstance(url, str) and url:
        if kind == "git" and isinstance(commit, str):
            return f"git {_redact_url(url)}@{commit.lower()}"
        return f"{kind} {_redact_url(url)}"
    if isinstance(path, str) and path:
        return f"{kind} {path}"
    paths = source.get("paths")
    if isinstance(paths, list) and paths:
        return f"{kind} {', '.join(_clean(item) for item in paths)}"
    if kind == "inline":
        name = source.get("dest-filename") or source.get("dest") or "inline"
        return f"inline {name}"
    if kind in {"shell", "script"}:
        commands = source.get("commands")
        if isinstance(commands, list) and commands:
            text = " ".join(_clean(command) for command in commands)
            return f"{kind} {text}"
        return kind
    return kind


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _with_digests(identity: str, digests: list[tuple[str, str]]) -> str:
    if not digests:
        return identity
    values = " ".join(f"{name}={value}" for name, value in digests)
    return f"{identity} {values}"


def _source_kind(source: dict[str, Any]) -> str:
    source_type = _clean(source.get("type") or "").lower()
    if source_type == "inline":
        return "generated"
    if source_type == "patch":
        return "patch"
    if source_type == "file" and source.get("path") and not source.get("url"):
        return "repository"
    return "upstream"


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _is_lfs_pointer(data: bytes) -> bool:
    return data.startswith(LFS_POINTER) and b"\noid sha256:" in data[:1024]


def _compression_magic(data: bytes) -> str:
    if data[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}:
        return "zip"
    if data[:2] == b"\x1f\x8b":
        return "gzip"
    if data[:3] == b"BZh":
        return "bz2"
    if data[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if data[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    return "none"


def _archive_format(source_type: str, data: bytes) -> str:
    value = source_type.lower()
    if value in ZIP_TYPES:
        return "zip"
    if value in TAR_TYPES:
        return "tar"
    if value == "archive":
        magic = _compression_magic(data)
        if magic == "zip":
            return "zip"
        if data.startswith(b"7z\xbc\xaf\x27\x1c"):
            raise _SourceError("unsupported archive type: 7z")
        if data.startswith(b"Rar!\x1a\x07"):
            raise _SourceError("unsupported archive type: rar")
        if data.startswith(b"LZIP"):
            raise _SourceError("unsupported archive type: lzip")
        if data.startswith(b"\x04\x22\x4d\x18"):
            raise _SourceError("unsupported archive type: lz4")
        if data.startswith(b"\x1f\x9d"):
            raise _SourceError("unsupported archive type: compress")
        return "tar"
    if value in UNSUPPORTED_ARCHIVE_TYPES:
        raise _SourceError(f"unsupported archive type: {source_type}")
    raise _SourceError(f"unsupported archive type: {source_type}")


def _zip_entry_count(data: bytes) -> int:
    if len(data) < 22:
        raise _SourceError("invalid zip archive")
    first = max(0, len(data) - 22 - 65535)
    for position in range(len(data) - 22, first - 1, -1):
        if data[position : position + 4] != b"PK\x05\x06":
            continue
        comment = int.from_bytes(data[position + 20 : position + 22], "little")
        if position + 22 + comment != len(data):
            continue
        count = int.from_bytes(data[position + 10 : position + 12], "little")
        if count != 0xFFFF:
            return count
        locator = position - 20
        if locator < 0 or data[locator : locator + 4] != b"PK\x06\x07":
            raise _SourceError("zip64 archive has no valid locator")
        zip64_position = int.from_bytes(data[locator + 8 : locator + 16], "little")
        if (
            zip64_position + 56 > len(data)
            or data[zip64_position : zip64_position + 4] != b"PK\x06\x06"
        ):
            raise _SourceError("zip64 archive has no valid end record")
        return int.from_bytes(data[zip64_position + 32 : zip64_position + 40], "little")
    raise _SourceError("invalid zip archive")


def _safe_link_target(relpath: str, linkname: str) -> str:
    if not isinstance(linkname, str) or not linkname:
        raise _SourceError("archive link has no target")
    if "\x00" in linkname:
        raise _SourceError("archive link target contains a NUL byte")
    text = linkname.replace("\\", "/")
    if text.startswith("/") or DRIVE_PATH.match(text):
        raise _SourceError(f"unsafe archive link target: {linkname}")
    base = posixpath.dirname(relpath)
    target = posixpath.normpath(posixpath.join(base, text))
    if target in {".", ".."} or target.startswith(("../", "/")):
        raise _SourceError(f"unsafe archive link target: {linkname}")
    return target


def _git_components(url: str) -> tuple[str, list[str]]:
    value = url.strip()
    match = SCP_GIT_URL.match(value)
    if match and "://" not in value:
        host = match.group("host").lower()
        path = match.group("path")
    else:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        if scheme not in {
            "http",
            "https",
            "git",
            "ssh",
            "git+ssh",
            "git+http",
            "git+https",
        }:
            raise _SourceError(f"unsupported git URL scheme: {parts.scheme}")
        if parts.password is not None:
            raise _SourceError("git URL credentials are not allowed")
        if (
            scheme in {"http", "https", "git+http", "git+https"}
            and parts.username is not None
        ):
            raise _SourceError("git URL credentials are not allowed")
        if parts.query or parts.fragment:
            raise _SourceError("git URL must not contain a query or fragment")
        try:
            port = parts.port
        except ValueError as err:
            raise _SourceError("git URL has an invalid port") from err
        allowed_ports = (
            (None, 80, 443)
            if scheme in {"http", "https", "git+http", "git+https"}
            else (None, 22, 9418)
        )
        if port not in allowed_ports:
            raise _SourceError("git URL with a custom port is not supported")
        host = parts.hostname
        if not host:
            raise _SourceError("git URL has no host")
        host = host.lower()
        path = parts.path
    path = unquote(path).strip("/")
    path = path.removesuffix(".git")
    parts = _safe_parts(path)
    if len(parts) < 2:
        raise _SourceError("git URL must name a repository")
    return host, parts


def _forge_archive_url(host: str, parts: list[str], commit: str) -> str:
    label = host.removeprefix("www.")
    quoted = "/".join(quote(part, safe="") for part in parts)
    repo = quote(parts[-1], safe="")
    if label == "github.com":
        if len(parts) != 2:
            raise _SourceError("GitHub git URL must be owner/repo")
        return f"https://codeload.github.com/{quoted}/tar.gz/{commit}"
    if label == "codeberg.org":
        return f"https://{label}/{quoted}/archive/{commit}.tar.gz"
    if label in GITLAB_HOSTS:
        return f"https://{label}/{quoted}/-/archive/{commit}/{repo}-{commit}.tar.gz"
    raise _SourceError(f"git host {host} has no supported pinned archive URL")


class SourceWorkspace:
    def __init__(
        self,
        manifest_path: str,
        repo_file: Callable[[str], Awaitable[str | None]],
        repo_bytes: Callable[[str], Awaitable[bytes | None]] | None = None,
    ):
        self._manifest_path = str(manifest_path)
        self._repo_file = repo_file
        self._repo_bytes = repo_bytes
        self._limits = _limits()
        self._tempdir = tempfile.TemporaryDirectory(
            prefix="vorarbeiter-sources-",
            ignore_cleanup_errors=True,
        )
        self._root = Path(self._tempdir.name) / "workspace"
        self._root.mkdir(mode=0o700)
        self._modules: list[_Module] = []
        self._module_roots: dict[str, Path] = {}
        self._file_index: dict[tuple[str, str], _File] = {}
        self._total_bytes = 0
        self._repository_module: _Module | None = None
        self._repo_cache: dict[str, str | None] = {}
        self._repo_data_cache: dict[str, bytes | None] = {}
        self._include_stack: list[str] = []
        self._include_count = 0
        self._include_results: list[_SourceResult] = []
        self._global_errors: list[str] = []
        self._source_count = 0
        self._entry_count = 0
        self._tree_limited = False
        self._cache = _DownloadCache(self._limits.cache_entries)
        self._closed = False

    @classmethod
    async def create(
        cls,
        manifest_path: str,
        manifest_text: str,
        repo_file: Callable[[str], Awaitable[str | None]],
        repo_bytes: Callable[[str], Awaitable[bytes | None]] | None = None,
    ) -> "SourceWorkspace":
        workspace = cls(manifest_path, repo_file, repo_bytes)
        try:
            await workspace._build(manifest_text)
        except Exception:
            workspace.close()
            raise
        return workspace

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._make_writable()
        self._tempdir.cleanup()

    def report(self) -> str:
        if self._closed:
            return "source workspace is closed"
        ok = skipped = errors = 0
        for module in self._modules:
            for result in module.results:
                if result.status == "ok":
                    ok += 1
                elif result.status == "skipped":
                    skipped += 1
                else:
                    errors += 1
        include_errors = sum(
            1 for result in self._include_results if result.status == "error"
        )
        lines = [
            (
                "source workspace: "
                f"{len(self._modules)} modules, "
                f"{self._source_count} sources "
                f"({ok} ok, {skipped} not materialized, {errors} errors), "
                f"{len(self._include_results)} includes "
                f"({include_errors} errors), "
                f"{len(self._file_index)} files, "
                f"{self._total_bytes} bytes"
            )
        ]
        for error in self._global_errors[:MAX_LISTED_PATHS]:
            lines.append(f"error: {_truncate(_clean(error), MAX_DETAIL_CHARS)}")
        omitted = len(self._global_errors) - MAX_LISTED_PATHS
        if omitted > 0:
            lines.append(f"error: ({omitted} more global errors omitted)")
        for module in self._modules:
            module_files = [
                file
                for file in self._file_index.values()
                if file.module_dir == module.dirname
            ]
            files = len(module_files)
            annotation = (
                " (repository copies of included manifest files)"
                if module is self._repository_module
                else ""
            )
            lines.append(f"module {_clean(module.name)}: {files} files{annotation}")
            if module_files:
                shown = ", ".join(
                    f"{file.relpath} ({file.kind})"
                    for file in module_files[:MAX_LISTED_FILES]
                )
                if len(module_files) > MAX_LISTED_FILES:
                    shown += f", and {len(module_files) - MAX_LISTED_FILES} more"
                lines.append(f"  files: {_truncate(shown, 2000)}")
            for error in module.errors[:MAX_LISTED_PATHS]:
                lines.append(f"  [error] {_truncate(_clean(error), MAX_DETAIL_CHARS)}")
            for result in sorted(
                module.results,
                key=lambda item: STATUS_ORDER.get(item.status, 3),
            ):
                marker = {
                    "error": "error",
                    "skipped": "not materialized",
                    "ok": "ok",
                }.get(result.status, result.status)
                identity = _truncate(_clean(result.identity), MAX_IDENTITY_CHARS)
                detail = _truncate(_clean(result.detail), MAX_DETAIL_CHARS)
                lines.append(f"  [{marker}] {identity} ({result.kind}): {detail}")
        for result in sorted(
            self._include_results,
            key=lambda item: STATUS_ORDER.get(item.status, 3),
        ):
            marker = "error" if result.status == "error" else "ok"
            identity = _truncate(_clean(result.identity), MAX_IDENTITY_CHARS)
            detail = _truncate(_clean(result.detail), MAX_DETAIL_CHARS)
            lines.append(f"  [{marker}] {identity}: {detail}")
        text = "\n".join(lines)
        if len(text) <= MAX_REPORT_CHARS:
            return text
        remainder = text[MAX_REPORT_CHARS:]
        omitted_errors = remainder.count("[error]")
        suffix = "\n(report truncated at the configured output limit"
        if omitted_errors:
            suffix += f"; {omitted_errors} error entries omitted"
        suffix += ")"
        return _truncate(text, MAX_REPORT_CHARS) + suffix

    def read(
        self,
        module: str,
        path: str,
        kind: str = "upstream",
        start_line: int = 1,
        end_line: int = MAX_READ_LINES,
    ) -> str:
        if self._closed:
            return "source workspace is closed"
        if kind not in KINDS:
            return f"unknown kind {kind}; kinds: {', '.join(KINDS)}"
        try:
            relpath = _safe_relpath(path)
        except _SourceError as err:
            return f"invalid path: {err}"
        modules = [item for item in self._modules if item.name == module]
        if not modules:
            names = ", ".join(dict.fromkeys(item.name for item in self._modules))
            return (
                f"module {module} not found; "
                f"modules: {_truncate(names, 2000) or 'none'}"
            )
        file: _File | None = None
        other_kind: _File | None = None
        for candidate_module in modules:
            candidate = self._file_index.get((candidate_module.dirname, relpath))
            if candidate is None:
                continue
            if candidate.kind == kind:
                file = candidate
                break
            other_kind = candidate
        if file is None:
            if other_kind is not None:
                return (
                    f"{relpath} is materialized with kind {other_kind.kind}, not {kind}"
                )
            available = sorted(
                candidate.relpath
                for candidate_module in modules
                for key, candidate in self._file_index.items()
                if key[0] == candidate_module.dirname
            )
            shown = ", ".join(available[:MAX_LISTED_PATHS]) or "none"
            suffix = ""
            if len(available) > MAX_LISTED_PATHS:
                suffix = f" ({len(available) - MAX_LISTED_PATHS} more)"
            return (
                f"{_truncate(relpath, 500)} is not materialized for module "
                f"{module} with kind {kind}; available files: "
                f"{_truncate(shown, 2000)}{suffix}"
            )
        try:
            start = max(1, int(start_line))
        except (TypeError, ValueError):
            return "start_line must be an integer"
        try:
            end = int(end_line)
        except (TypeError, ValueError):
            return "end_line must be an integer"
        if end < start:
            return "end_line must not be less than start_line"
        requested_end = end
        end = min(end, start + MAX_READ_LINES - 1)
        header = (
            f"module: {_clean(file.module)} | kind: {file.kind} | "
            f"source: {_truncate(_clean(file.source), MAX_IDENTITY_CHARS)} | "
            f"path: {_truncate(file.relpath, 500)}"
        )
        resolved = self._readable_path(file)
        if resolved is None:
            return f"{relpath} is not safely readable"
        try:
            with resolved.open("rb") as handle:
                data = handle.read(self._limits.read_max_bytes + 1)
        except OSError as err:
            return f"could not read {relpath}: {err}"
        byte_truncated = len(data) > self._limits.read_max_bytes
        if byte_truncated:
            data = data[: self._limits.read_max_bytes]
        if _is_binary(data):
            return f"{header}\n(binary file, {file.size} bytes; not shown)"
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if not lines:
            return f"{header}\n(empty file)"
        if start > len(lines):
            return (
                f"{header}\n(file has {len(lines)} lines; start_line "
                f"{start} is beyond the end)"
            )
        end = min(end, len(lines))
        output = [header]
        for number in range(start, end + 1):
            line = lines[number - 1]
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + " [line truncated]"
            output.append(f"{number}: {line}")
        if byte_truncated:
            output.append(f"(read truncated at {self._limits.read_max_bytes} bytes)")
        else:
            output.append(f"(file has {len(lines)} lines)")
        if requested_end > end and end < len(lines) and not byte_truncated:
            output.append(f"(read window ended at line {end})")
        return "\n".join(output)

    def search(
        self,
        text: str,
        module: str | None = None,
        kind: str = "upstream",
        path_prefix: str = "",
    ) -> str:
        if self._closed:
            return "source workspace is closed"
        if not isinstance(text, str) or not text:
            return "search text is empty"
        if kind not in KINDS:
            return f"unknown kind {kind}; kinds: {', '.join(KINDS)}"
        try:
            prefix = (
                _safe_relpath(path_prefix) if path_prefix not in {"", ".", "./"} else ""
            )
        except _SourceError as err:
            return f"invalid path_prefix: {err}"
        modules = [
            item for item in self._modules if module is None or item.name == module
        ]
        if module is not None and not modules:
            names = ", ".join(dict.fromkeys(item.name for item in self._modules))
            return (
                f"module {module} not found; "
                f"modules: {_truncate(names, 2000) or 'none'}"
            )
        needle = text.casefold()
        hits: list[str] = []
        scanned_bytes = 0
        scanned_files = 0
        skipped_unsafe = 0
        stop_reason = ""
        for candidate_module in modules:
            for file in self._file_index.values():
                if file.module_dir != candidate_module.dirname:
                    continue
                if file.kind != kind:
                    continue
                if prefix and not (
                    file.relpath == prefix
                    or file.relpath.startswith(prefix.rstrip("/") + "/")
                ):
                    continue
                if file.binary:
                    continue
                if scanned_files >= self._limits.search_max_files:
                    stop_reason = "file limit"
                    break
                resolved = self._readable_path(file)
                if resolved is None:
                    skipped_unsafe += 1
                    continue
                remaining = self._limits.search_max_bytes - scanned_bytes
                if remaining <= 0:
                    stop_reason = "byte limit"
                    break
                try:
                    with resolved.open("rb") as handle:
                        data = handle.read(min(file.size, remaining) + 1)
                except OSError:
                    continue
                partial = len(data) > remaining
                if partial:
                    data = data[:remaining]
                scanned_bytes += len(data)
                scanned_files += 1
                if _is_binary(data):
                    continue
                lines = data.decode("utf-8", errors="replace").splitlines()
                for number, line in enumerate(lines, 1):
                    if needle not in line.casefold():
                        continue
                    source = _truncate(_clean(file.source), 120)
                    shown = line
                    if len(shown) > MAX_LINE_CHARS:
                        shown = shown[:MAX_LINE_CHARS] + " [line truncated]"
                    hits.append(
                        f"module={_clean(file.module)} kind={file.kind} "
                        f"path={_truncate(file.relpath, 300)}:{number}: "
                        f"{shown} [source: {source}]"
                    )
                    if len(hits) >= MAX_SEARCH_HITS:
                        stop_reason = "hit limit"
                        break
                if stop_reason:
                    break
                if partial:
                    stop_reason = "byte limit"
                    break
            if stop_reason:
                break
        output = list(hits)
        if not output:
            output.append(f"no hits for {_truncate(text, 200)!r}")
        if stop_reason == "hit limit":
            output.append(f"(hit limit reached; only {MAX_SEARCH_HITS} hits shown)")
        elif stop_reason:
            output.append(
                f"(search stopped at the {stop_reason}: "
                f"{scanned_files} files, {scanned_bytes} bytes scanned)"
            )
        if skipped_unsafe:
            output.append(f"(skipped {skipped_unsafe} unsafe paths)")
        joined = "\n".join(output)
        if len(joined) > MAX_SEARCH_OUTPUT_CHARS:
            return _truncate(joined, MAX_SEARCH_OUTPUT_CHARS) + "\n(output truncated)"
        return joined

    async def _build(self, manifest_text: str) -> None:
        try:
            manifest_path = _safe_relpath(self._manifest_path)
        except _SourceError as err:
            self._global_errors.append(f"manifest path: {err}")
            manifest_path = posixpath.basename(self._manifest_path.replace("\\", "/"))
        self._manifest_path = manifest_path or "manifest.json"
        manifest = _parse_manifest(self._manifest_path, manifest_text)
        if not isinstance(manifest, dict) or not manifest:
            self._global_errors.append("root manifest is empty or could not be parsed")
            manifest = {}
        self._add_repository_file(self._manifest_path, manifest_text)
        base = posixpath.dirname(self._manifest_path)
        modules = manifest.get("modules")
        await self._process_modules(modules, base, 0)
        if manifest.get("sources") is not None:
            module = self._new_module("root")
            await self._process_sources(manifest.get("sources"), module, base, 0)
        self._make_read_only()

    def _manifest_module(self) -> _Module:
        if self._repository_module is None:
            self._repository_module = self._new_module("manifest")
        return self._repository_module

    def _new_module(self, name: str) -> _Module:
        display = _clean(name).strip() or "module"
        base = _module_dirname(display)
        candidate = base
        suffix = 2
        while (self._root / candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        root = self._root / candidate
        root.mkdir(mode=0o700)
        module = _Module(name=display, dirname=candidate, root=root)
        self._modules.append(module)
        self._module_roots[candidate] = root
        return module

    def _record_include(self, path: str, status: str, detail: str) -> None:
        self._include_results.append(
            _SourceResult(
                kind="repository",
                identity=f"include {path}",
                status=status,
                detail=detail,
            )
        )

    def _record_module_error(self, module: _Module, message: str) -> None:
        module.errors.append(message)

    def _note_tree_limit(self, message: str, module: _Module | None = None) -> None:
        if self._tree_limited:
            return
        self._tree_limited = True
        if module is not None:
            module.errors.append(message)
        else:
            self._global_errors.append(message)

    def _add_repository_file(self, path: str, text: str) -> None:
        try:
            relpath = _safe_relpath(path)
        except _SourceError as err:
            self._global_errors.append(f"repository file {path}: {err}")
            return
        module = self._manifest_module()
        if (module.dirname, relpath) in self._file_index:
            return
        try:
            self._write_bytes(
                module,
                relpath,
                text.encode("utf-8"),
                "repository",
                f"repository:{relpath}",
            )
        except _SourceError as err:
            self._global_errors.append(f"repository file {relpath}: {err}")

    async def _repo_text(self, path: str) -> str:
        if path in self._repo_cache:
            value = self._repo_cache[path]
            if value is None:
                raise _SourceError(f"repository file not found: {path}")
            return value
        try:
            value = await self._repo_file(path)
        except Exception as err:
            raise _SourceError(
                f"repository file {path}: {type(err).__name__}: {err}"
            ) from err
        if value is not None and not isinstance(value, str):
            raise _SourceError(f"repository file {path}: repo_file returned non-text")
        self._repo_cache[path] = value
        if value is None:
            raise _SourceError(f"repository file not found: {path}")
        return value

    async def _repo_data(self, path: str) -> bytes:
        if self._repo_bytes is None:
            return (await self._repo_text(path)).encode("utf-8")
        if path in self._repo_data_cache:
            value = self._repo_data_cache[path]
            if value is None:
                raise _SourceError(f"repository file not found: {path}")
            return value
        try:
            value = await self._repo_bytes(path)
        except Exception as err:
            raise _SourceError(
                f"repository file {path}: {type(err).__name__}: {err}"
            ) from err
        if value is not None and not isinstance(value, bytes):
            raise _SourceError(f"repository file {path}: repo_bytes returned non-bytes")
        self._repo_data_cache[path] = value
        if value is None:
            raise _SourceError(f"repository file not found: {path}")
        return value

    @asynccontextmanager
    async def _included(self, path: str, depth: int):
        if depth >= MAX_INCLUDE_DEPTH:
            raise _SourceError("maximum include depth exceeded")
        if path in self._include_stack:
            chain = " -> ".join([*self._include_stack, path])
            raise _SourceError(f"include cycle: {chain}")
        if self._include_count >= MAX_INCLUDES:
            raise _SourceError("maximum include count exceeded")
        self._include_count += 1
        text = await self._repo_text(path)
        fragment = _parse_fragment(path, text)
        self._add_repository_file(path, text)
        self._record_include(path, "ok", "loaded")
        self._include_stack.append(path)
        try:
            yield fragment
        finally:
            self._include_stack.pop()

    async def _process_modules(
        self,
        value: Any,
        base: str,
        depth: int,
        parent: _Module | None = None,
    ) -> None:
        if value is None:
            return
        if depth >= MAX_INCLUDE_DEPTH:
            self._note_tree_limit("maximum manifest nesting depth exceeded", parent)
            return
        items = value if isinstance(value, list) else [value]
        for item in items:
            self._entry_count += 1
            if self._entry_count > MAX_TREE_ENTRIES:
                self._note_tree_limit("maximum manifest entry count exceeded", parent)
                return
            try:
                if isinstance(item, str):
                    await self._process_module_include(item, base, depth)
                elif isinstance(item, dict):
                    await self._process_module_dict(item, base, depth)
                else:
                    raise _SourceError("module entry is not an object or path")
            except _SourceError as err:
                if parent is not None:
                    self._record_module_error(parent, str(err))
                else:
                    self._global_errors.append(f"module: {err}")
            except Exception as err:
                logger.exception("Unexpected module materialization failure")
                message = f"unexpected {type(err).__name__}: {err}"
                if parent is not None:
                    self._record_module_error(parent, message)
                else:
                    self._global_errors.append(f"module: {message}")

    async def _process_module_dict(
        self,
        item: dict[str, Any],
        base: str,
        depth: int,
        default_name: str | None = None,
    ) -> None:
        path = item.get("path")
        if (
            isinstance(path, str)
            and "name" not in item
            and "sources" not in item
            and "modules" not in item
        ):
            await self._process_module_include(path, base, depth)
            return
        if "type" in item and "sources" not in item and "modules" not in item:
            raise _SourceError("module fragment contains a source entry")
        name = item.get("name", default_name or "module")
        module = self._new_module(str(name))
        await self._process_sources(item.get("sources"), module, base, depth)
        if item.get("modules") is not None:
            await self._process_modules(item.get("modules"), base, depth + 1, module)

    async def _process_module_include(
        self,
        path: str,
        base: str,
        depth: int,
    ) -> None:
        try:
            resolved = _join_repo_path(base, path)
        except _SourceError as err:
            self._record_include(path, "error", str(err))
            return
        try:
            async with self._included(resolved, depth) as fragment:
                await self._process_module_fragment(fragment, resolved, depth + 1)
        except _SourceError as err:
            self._record_include(resolved, "error", str(err))
        except Exception as err:
            logger.exception("Unexpected module include failure")
            self._record_include(
                resolved,
                "error",
                f"unexpected {type(err).__name__}: {err}",
            )

    async def _process_module_fragment(
        self,
        fragment: Any,
        path: str,
        depth: int,
    ) -> None:
        base = posixpath.dirname(path)
        if fragment is None:
            return
        if isinstance(fragment, list):
            await self._process_modules(fragment, base, depth)
            return
        if not isinstance(fragment, dict):
            raise _SourceError(f"module fragment {path} is not an object or list")
        if (
            "modules" in fragment
            and "name" not in fragment
            and "sources" not in fragment
        ):
            await self._process_modules(fragment.get("modules"), base, depth)
            return
        await self._process_module_dict(fragment, base, depth, default_name=_stem(path))

    async def _process_source_include(
        self,
        path: str,
        base: str,
        module: _Module,
        depth: int,
    ) -> None:
        try:
            resolved = _join_repo_path(base, path)
        except _SourceError as err:
            self._record_include(path, "error", str(err))
            return
        try:
            async with self._included(resolved, depth) as fragment:
                await self._process_source_fragment(
                    fragment, resolved, module, depth + 1
                )
        except _SourceError as err:
            self._record_include(resolved, "error", str(err))
        except Exception as err:
            logger.exception("Unexpected source include failure")
            self._record_include(
                resolved,
                "error",
                f"unexpected {type(err).__name__}: {err}",
            )

    async def _process_source_fragment(
        self,
        fragment: Any,
        path: str,
        module: _Module,
        depth: int,
    ) -> None:
        base = posixpath.dirname(path)
        if fragment is None:
            return
        if isinstance(fragment, list):
            await self._process_sources(fragment, module, base, depth)
            return
        if not isinstance(fragment, dict):
            raise _SourceError(f"source fragment {path} is not an object or list")
        if not fragment:
            return
        if fragment.get("type") is not None:
            await self._process_source(fragment, module, base)
            return
        if "name" in fragment and ("sources" in fragment or "modules" in fragment):
            await self._process_module_dict(fragment, base, depth)
            return
        if "sources" in fragment or "modules" in fragment:
            await self._process_sources(
                fragment.get("sources"), module, base, depth + 1
            )
            await self._process_modules(
                fragment.get("modules"), base, depth + 1, module
            )
            return
        if set(fragment) == {"path"} and isinstance(fragment["path"], str):
            await self._process_source_include(fragment["path"], base, module, depth)
            return
        await self._process_source(fragment, module, base)

    async def _process_sources(
        self,
        value: Any,
        module: _Module,
        base: str,
        depth: int,
    ) -> None:
        if value is None:
            return
        if depth >= MAX_INCLUDE_DEPTH:
            self._note_tree_limit("maximum manifest nesting depth exceeded", module)
            return
        items = value if isinstance(value, list) else [value]
        for item in items:
            self._entry_count += 1
            if self._entry_count > MAX_TREE_ENTRIES:
                self._note_tree_limit("maximum manifest entry count exceeded", module)
                return
            if isinstance(item, str):
                await self._process_source_include(item, base, module, depth)
                continue
            if not isinstance(item, dict):
                self._record_module_error(
                    module, "source entry is not an object or path"
                )
                continue
            if item.get("type") is None and ("sources" in item or "modules" in item):
                if "name" in item:
                    try:
                        await self._process_module_dict(item, base, depth)
                    except _SourceError as err:
                        self._record_module_error(module, str(err))
                    continue
                await self._process_sources(
                    item.get("sources"), module, base, depth + 1
                )
                await self._process_modules(
                    item.get("modules"), base, depth + 1, module
                )
                continue
            await self._process_source(item, module, base)

    async def _process_source(
        self,
        source: dict[str, Any],
        module: _Module,
        base: str,
    ) -> None:
        if self._source_count >= MAX_SOURCE_ENTRIES:
            if not module.source_limit_reported:
                module.source_limit_reported = True
                self._record_module_error(
                    module,
                    f"source limit reached ({MAX_SOURCE_ENTRIES}); "
                    "additional sources were not attempted",
                )
            return
        self._source_count += 1
        identity = _source_identity(source)
        kind = _source_kind(source)
        try:
            outcome = await self._materialize_source(source, module, base)
        except _SourceError as err:
            module.results.append(_SourceResult(kind, identity, "error", str(err)))
        except Exception as err:
            logger.exception("Unexpected source materialization failure")
            module.results.append(
                _SourceResult(
                    kind,
                    identity,
                    "error",
                    f"unexpected {type(err).__name__}: {err}",
                )
            )
        else:
            details = []
            if outcome.detail:
                details.append(outcome.detail)
            details.extend(outcome.notices)
            module.results.append(
                _SourceResult(
                    kind,
                    identity,
                    outcome.status,
                    "; ".join(details),
                )
            )

    async def _materialize_source(
        self,
        source: dict[str, Any],
        module: _Module,
        base: str,
    ) -> _Outcome:
        raw_type = source.get("type")
        if not isinstance(raw_type, str) or not raw_type:
            raise _SourceError("source has no type")
        source_type = raw_type.lower()
        if source_type == "inline":
            return self._inline_source(source, module)
        if source_type in {"shell", "script"}:
            return _Outcome(
                "skipped",
                f"{source_type} source is never executed; "
                "commands were not materialized",
            )
        if source_type in {"extra-data", "dir"}:
            return _Outcome("skipped", f"{source_type} source is not materialized")
        if source_type in {"bzr", "svn"}:
            return _Outcome("skipped", f"{source_type} sources are not materialized")
        if source_type == "patch":
            return await self._patch_source(source, module, base)
        if source_type == "git":
            return await self._git_source(source, module)
        if source_type == "file":
            return await self._file_source(source, module, base)
        if (
            source_type == "archive"
            or source_type in TAR_TYPES
            or source_type in ZIP_TYPES
        ):
            return await self._archive_source(source, module, source_type)
        if source_type in UNSUPPORTED_ARCHIVE_TYPES:
            raise _SourceError(f"unsupported archive type: {source_type}")
        raise _SourceError(f"unsupported source type: {source_type}")

    def _inline_source(
        self,
        source: dict[str, Any],
        module: _Module,
    ) -> _Outcome:
        contents = source.get("contents")
        if not isinstance(contents, str):
            raise _SourceError("inline source requires text contents")
        name = source.get("dest-filename")
        if not isinstance(name, str) or not name:
            raise _SourceError("inline source requires dest-filename")
        data = contents.encode("utf-8")
        if source.get("base64"):
            try:
                data = base64.b64decode(contents, validate=True)
            except (ValueError, binascii.Error) as err:
                raise _SourceError("inline source has invalid base64") from err
        dest = _dest_path(source, name)
        identity = f"inline {name}"
        self._write_bytes(module, dest, data, "generated", identity)
        return _Outcome("ok", f"wrote {dest}")

    async def _file_source(
        self,
        source: dict[str, Any],
        module: _Module,
        base: str,
    ) -> _Outcome:
        url = source.get("url")
        path = source.get("path")
        if isinstance(url, str) and url:
            download = await self._remote_bytes(source)
            identity = _with_digests(_source_identity(source), download.digests)
            dest = _dest_path(source, _url_filename(url))
            self._write_bytes(module, dest, download.data, "upstream", identity)
            detail = f"downloaded {dest} from {download.url}"
            if download.attempts:
                detail += "; " + "; ".join(download.attempts)
            return _Outcome("ok", detail)
        if isinstance(path, str) and path:
            repo_path = _join_repo_path(base, path)
            data = await self._repo_data(repo_path)
            dest = _dest_path(source, posixpath.basename(repo_path))
            self._write_bytes(
                module,
                dest,
                data,
                "repository",
                f"repository:{repo_path}",
            )
            return _Outcome("ok", f"copied {dest} from {repo_path}")
        raise _SourceError("file source requires a url or path")

    async def _patch_source(
        self,
        source: dict[str, Any],
        module: _Module,
        base: str,
    ) -> _Outcome:
        contents = source.get("contents")
        path = source.get("path")
        paths = source.get("paths")
        url = source.get("url")
        if paths is not None:
            if not isinstance(paths, list) or not paths:
                raise _SourceError("patch paths must be a non-empty list")
            if source.get("dest-filename") is not None:
                raise _SourceError("dest-filename is not valid for patch paths")
            stored = []
            for item in paths:
                if not isinstance(item, str) or not item:
                    raise _SourceError("patch paths must contain repository paths")
                repo_path = _join_repo_path(base, item)
                text = await self._repo_text(repo_path)
                dest = _dest_path(source, posixpath.basename(repo_path))
                self._write_bytes(
                    module,
                    dest,
                    text.encode("utf-8"),
                    "patch",
                    f"patch repository:{repo_path}",
                )
                stored.append(dest)
            return _Outcome("ok", f"stored {', '.join(stored)} unapplied")
        if isinstance(contents, str):
            dest = _dest_path(source, "patch.diff")
            self._write_bytes(
                module,
                dest,
                contents.encode("utf-8"),
                "patch",
                "patch inline",
            )
            return _Outcome("ok", f"stored {dest} unapplied")
        if isinstance(path, str) and path:
            repo_path = _join_repo_path(base, path)
            text = await self._repo_text(repo_path)
            dest = _dest_path(source, posixpath.basename(repo_path))
            self._write_bytes(
                module,
                dest,
                text.encode("utf-8"),
                "patch",
                f"patch repository:{repo_path}",
            )
            return _Outcome("ok", f"stored {dest} unapplied")
        if isinstance(url, str) and url:
            download = await self._remote_bytes(source)
            identity = _with_digests(f"patch {_redact_url(url)}", download.digests)
            dest = _dest_path(source, _url_filename(url))
            self._write_bytes(module, dest, download.data, "patch", identity)
            detail = f"stored {dest} unapplied from {download.url}"
            if download.attempts:
                detail += "; " + "; ".join(download.attempts)
            return _Outcome("ok", detail)
        raise _SourceError("patch source requires contents, path, paths, or url")

    async def _archive_source(
        self,
        source: dict[str, Any],
        module: _Module,
        source_type: str,
    ) -> _Outcome:
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise _SourceError("archive source requires a url")
        download = await self._remote_bytes(source)
        identity = _with_digests(_source_identity(source), download.digests)
        dest = _dest_dir(source)
        strip = _int_field(source, "strip-components", 1)
        stats, _files = self._extract_archive(
            download.data,
            source_type,
            module,
            identity,
            dest,
            strip,
            "upstream",
        )
        notices: list[str] = []
        if source.get("dest-filename") is not None:
            notices.append("dest-filename is not applied to archive sources")
        detail = (
            f"extracted {stats.files} files ({stats.bytes} bytes) from {download.url}"
        )
        if download.attempts:
            detail += "; " + "; ".join(download.attempts)
        return _Outcome("ok", detail, notices=notices)

    async def _git_source(
        self,
        source: dict[str, Any],
        module: _Module,
    ) -> _Outcome:
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise _SourceError("git source requires a url")
        commit = source.get("commit")
        if not isinstance(commit, str) or not FULL_COMMIT.fullmatch(commit):
            if source.get("tag") or source.get("branch"):
                raise _SourceError(
                    "git source is not pinned; tags and branches are not resolved"
                )
            raise _SourceError("git source requires a full 40-hex commit")
        commit = commit.lower()
        host, parts = _git_components(url)
        archive_url = _forge_archive_url(host, parts, commit)
        download = await self._git_archive(source, archive_url, commit)
        identity = f"git {_redact_url(url)}@{commit}"
        dest = _dest_dir(source)
        strip = _int_field(source, "strip-components", 0)
        stats, files = self._extract_archive(
            download.data,
            "tar.gz",
            module,
            identity,
            dest,
            strip + 1,
            "upstream",
        )
        problems, notices = self._git_notices(
            files,
            submodules_disabled=source.get("disable-submodules") is True,
            lfs_disabled=source.get("disable-lfs") is True,
        )
        detail = (
            f"extracted {stats.files} files from pinned commit "
            f"{commit} at {download.url}"
        )
        if download.attempts:
            detail += "; " + "; ".join(download.attempts)
        return _Outcome(
            "error" if problems else "ok", detail, notices=[*problems, *notices]
        )

    async def _git_archive(
        self,
        source: dict[str, Any],
        primary_url: str,
        commit: str,
    ) -> _Download:
        urls = [primary_url]
        attempts: list[str] = []
        mirrors = source.get("mirror-urls")
        if mirrors is not None:
            if isinstance(mirrors, str):
                mirrors = [mirrors]
            if not isinstance(mirrors, list):
                raise _SourceError("mirror-urls must be a list")
            for mirror in mirrors:
                if not isinstance(mirror, str) or not mirror:
                    attempts.append(f"invalid mirror-urls entry: {mirror!r}")
                    continue
                try:
                    host, parts = _git_components(mirror)
                    archive_url = _forge_archive_url(host, parts, commit)
                except _SourceError as err:
                    attempts.append(f"{_redact_url(mirror)}: {err}")
                    continue
                if archive_url not in urls:
                    urls.append(archive_url)
        if len(urls) > MAX_DOWNLOAD_URLS:
            attempts.append(
                f"{len(urls) - MAX_DOWNLOAD_URLS} fallback URLs were not attempted"
            )
            urls = urls[:MAX_DOWNLOAD_URLS]
        for index, url in enumerate(urls):
            try:
                download = await self._pinned_download(url)
            except _SourceError as err:
                attempts.append(f"{_redact_url(url)}: {err}")
                continue
            attempts.extend(download.attempts)
            if index:
                attempts.append(f"downloaded from {_redact_url(url)}")
            return _Download(download.data, download.url, attempts, [])
        raise _SourceError(
            "all git archive attempts failed: " + _truncate("; ".join(attempts), 2000)
        )

    async def _remote_bytes(self, source: dict[str, Any]) -> _Download:
        primary = source.get("url")
        if not isinstance(primary, str) or not primary:
            raise _SourceError("source requires a url")
        digests = _declared_digests(source)
        if not digests:
            raise _SourceError(
                "remote archive or file source has no digest "
                "(sha256/sha512/sha1/md5); refusing mutable content"
            )
        key = _cache_key(digests, primary)
        try:
            _validate_download_url(primary)
        except _SourceError:
            cached = None
        else:
            cached = await asyncio.to_thread(
                self._cache.get,
                key,
                digests,
                self._limits.download_max_bytes,
            )
        if cached is not None:
            return _Download(cached, _redact_url(primary), ["cache hit"], digests)
        raw_urls: list[Any] = [primary]
        mirrors = source.get("mirror-urls")
        attempts: list[str] = []
        if mirrors is not None:
            if isinstance(mirrors, str):
                mirrors = [mirrors]
            if not isinstance(mirrors, list):
                raise _SourceError("mirror-urls must be a list")
            for mirror in mirrors:
                if isinstance(mirror, str) and mirror:
                    raw_urls.append(mirror)
                else:
                    attempts.append(f"invalid mirror-urls entry: {mirror!r}")
        if len(raw_urls) > MAX_DOWNLOAD_URLS:
            attempts.append(
                f"{len(raw_urls) - MAX_DOWNLOAD_URLS} fallback URLs were not attempted"
            )
            raw_urls = raw_urls[:MAX_DOWNLOAD_URLS]
        for index, raw_url in enumerate(raw_urls):
            url = str(raw_url)
            try:
                data = await self._download_with_timeout(url)
                self._check_download_size(data)
                _verify_digests(data, digests)
            except _SourceError as err:
                attempts.append(f"{_redact_url(url)}: {err}")
                continue
            await asyncio.to_thread(self._cache.put, key, data, digests)
            if index:
                attempts.append(f"downloaded from {_redact_url(url)}")
            return _Download(data, _redact_url(url), attempts, digests)
        raise _SourceError(
            "all download attempts failed: " + _truncate("; ".join(attempts), 2000)
        )

    async def _pinned_download(self, url: str) -> _Download:
        key = _cache_key([], url)
        cached = await asyncio.to_thread(
            self._cache.get,
            key,
            [],
            self._limits.download_max_bytes,
        )
        if cached is not None:
            return _Download(cached, _redact_url(url), ["cache hit"], [])
        data = await self._download_with_timeout(url)
        self._check_download_size(data)
        await asyncio.to_thread(self._cache.put, key, data, [])
        return _Download(data, _redact_url(url), [], [])

    def _check_download_size(self, data: bytes) -> None:
        if len(data) > self._limits.download_max_bytes:
            raise _SourceError(
                f"download exceeds {self._limits.download_max_bytes} bytes"
            )

    async def _download_with_timeout(self, url: str) -> bytes:
        timeout = self._limits.timeout_seconds
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _download_once,
                    url,
                    self._limits.download_max_bytes,
                    timeout,
                ),
                timeout=timeout,
            )
        except TimeoutError as err:
            raise _SourceError(f"download timed out after {timeout:g} seconds") from err

    def _write_bytes(
        self,
        module: _Module,
        relpath: str,
        data: bytes,
        kind: str,
        source: str,
    ) -> _File:
        target = self._safe_target(module.root, relpath, allow_final_symlink=True)
        key = (module.dirname, relpath)
        existing = self._file_index.get(key)
        previous = existing.size if existing is not None else 0
        new_total = self._total_bytes - previous + len(data)
        if new_total > self._limits.extracted_max_bytes:
            raise _SourceError("materialized content exceeds the configured byte limit")
        if existing is None and len(self._file_index) >= self._limits.max_files:
            raise _SourceError("materialized file count exceeds the configured limit")
        with target.open("wb") as handle:
            handle.write(data)
        file = _File(
            module=module.name,
            module_dir=module.dirname,
            relpath=relpath,
            path=target,
            kind=kind,
            source=source,
            size=len(data),
            binary=_is_binary(data),
        )
        self._file_index[key] = file
        self._total_bytes = new_total
        return file

    def _safe_target(
        self,
        root: Path,
        relpath: str,
        allow_final_symlink: bool = False,
    ) -> Path:
        parts = _safe_parts(relpath)
        current = root
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise _SourceError(f"path traverses a symlink: {relpath}")
            if current.exists():
                if not current.is_dir():
                    raise _SourceError(f"path component is not a directory: {relpath}")
            else:
                current.mkdir(mode=0o700)
        target = current / parts[-1]
        if target.is_symlink():
            if allow_final_symlink:
                target.unlink()
            else:
                raise _SourceError(f"path is a symlink: {relpath}")
        elif allow_final_symlink and target.exists():
            if target.is_file():
                target.unlink()
            else:
                raise _SourceError(f"path is a directory: {relpath}")
        elif target.exists() and target.is_dir():
            raise _SourceError(f"path is a directory: {relpath}")
        return target

    def _readable_path(self, file: _File) -> Path | None:
        root = self._module_roots.get(file.module_dir)
        if root is None:
            return None
        try:
            resolved = file.path.resolve(strict=True)
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    def _extract_archive(
        self,
        data: bytes,
        source_type: str,
        module: _Module,
        identity: str,
        dest: str,
        strip: int,
        kind: str,
    ) -> tuple[_ArchiveStats, list[_File]]:
        stage = self._root / ".staging" / uuid.uuid4().hex
        stage.mkdir(mode=0o700, parents=True)
        stats = _ArchiveStats()
        try:
            archive_format = _archive_format(source_type, data)
            if archive_format == "zip":
                self._stage_zip(data, stage, strip, stats)
            else:
                self._stage_tar(data, source_type, stage, strip, stats)
            files = self._merge_stage(stage, module, dest, identity, kind)
        except _SourceError:
            raise
        except Exception as err:
            raise _SourceError(
                f"could not extract archive ({type(err).__name__}): {err}"
            ) from err
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return stats, files

    def _stage_tar(
        self,
        data: bytes,
        source_type: str,
        stage: Path,
        strip: int,
        stats: _ArchiveStats,
    ) -> None:
        raw = self._tar_reader(data, source_type)
        limit = self._decompression_limit(stats)
        reader: Any = _LimitedReader(raw, limit)
        try:
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                pending_links: list[tuple[str, str, str]] = []
                for member in archive:
                    parts = _safe_parts(member.name, allow_empty=True)
                    self._count_entry(stats)
                    if len(parts) <= strip:
                        continue
                    relpath = "/".join(parts[strip:])
                    if not relpath:
                        continue
                    if member.isdir():
                        self._stage_directory(stage, relpath)
                        continue
                    if member.issym():
                        _safe_link_target(relpath, member.linkname)

                        pending_links.append(("symlink", relpath, member.linkname))
                        continue
                    if member.islnk():
                        target_parts = _safe_parts(member.linkname)
                        if len(target_parts) <= strip:
                            raise _SourceError(
                                f"unsafe archive hardlink target: {member.linkname}"
                            )
                        target = "/".join(target_parts[strip:])

                        pending_links.append(("hardlink", relpath, target))
                        continue
                    if (
                        member.ischr()
                        or member.isblk()
                        or member.isfifo()
                        or member.isdev()
                    ):
                        raise _SourceError(f"unsafe archive member type: {member.name}")
                    if not member.isfile():
                        raise _SourceError(f"unsupported archive member: {member.name}")

                    self._stage_regular(
                        stage,
                        relpath,
                        archive.extractfile(member),
                        member.size,
                        stats,
                    )
                for link_type, relpath, target in pending_links:
                    if link_type == "symlink":
                        self._stage_symlink(stage, relpath, target)
                    else:
                        self._stage_hardlink(stage, relpath, target)
        finally:
            reader.close()

    def _stage_zip(
        self,
        data: bytes,
        stage: Path,
        strip: int,
        stats: _ArchiveStats,
    ) -> None:
        entries = _zip_entry_count(data)
        if len(self._file_index) + entries > self._limits.max_files:
            raise _SourceError("archive file count exceeds the configured limit")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) != entries:
                raise _SourceError("zip archive entry count is inconsistent")
            pending_links: list[tuple[str, str, str]] = []
            for info in infos:
                if info.flag_bits & 0x1:
                    raise _SourceError("encrypted zip archives are unsupported")
                parts = _safe_parts(info.filename, allow_empty=True)
                self._count_entry(stats)
                if len(parts) <= strip:
                    continue
                relpath = "/".join(parts[strip:])
                if not relpath:
                    continue
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir() or info.filename.endswith("/"):
                    self._stage_directory(stage, relpath)
                    continue
                if stat.S_ISDIR(mode):
                    self._stage_directory(stage, relpath)
                    continue
                if stat.S_ISLNK(mode):
                    with archive.open(info) as handle:
                        target_bytes = handle.read(4097)
                    if len(target_bytes) > 4096:
                        raise _SourceError(
                            f"archive symlink target is too long: {info.filename}"
                        )
                    try:
                        target_text = target_bytes.decode("utf-8")
                    except UnicodeDecodeError as err:
                        raise _SourceError(
                            f"archive symlink target is not UTF-8: {info.filename}"
                        ) from err
                    _safe_link_target(relpath, target_text)

                    pending_links.append(("symlink", relpath, target_text))
                    continue
                file_type = stat.S_IFMT(mode)
                if file_type in {
                    stat.S_IFCHR,
                    stat.S_IFBLK,
                    stat.S_IFIFO,
                    stat.S_IFSOCK,
                }:
                    raise _SourceError(f"unsafe archive member type: {info.filename}")
                if file_type not in {0, stat.S_IFREG}:
                    raise _SourceError(f"unsupported archive member: {info.filename}")

                with archive.open(info) as handle:
                    self._stage_regular(stage, relpath, handle, info.file_size, stats)
            for link_type, relpath, target in pending_links:
                if link_type == "symlink":
                    self._stage_symlink(stage, relpath, target)
                else:
                    self._stage_hardlink(stage, relpath, target)

    def _tar_reader(self, data: bytes, source_type: str) -> Any:
        value = source_type.lower()
        magic = _compression_magic(data)
        if value in {"tar.zst", "tzst"} or (
            value in {"archive", "tar"} and magic == "zstd"
        ):
            try:
                import zstandard
            except ImportError as err:
                raise _SourceError("tar.zst support is unavailable") from err
            try:
                return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data))
            except zstandard.ZstdError as err:
                raise _SourceError(
                    f"could not decompress tar.zst archive: {err}"
                ) from err
        if value in {"tar.gz", "tgz"} or (
            value in {"archive", "tar"} and magic == "gzip"
        ):
            return gzip.GzipFile(fileobj=io.BytesIO(data))
        if value in {"tar.bz2", "tbz2"} or (
            value in {"archive", "tar"} and magic == "bz2"
        ):
            return bz2.BZ2File(io.BytesIO(data))
        if value in {"tar.xz", "txz", "tar.lzma", "tlz"} or (
            value in {"archive", "tar"} and magic == "xz"
        ):
            return lzma.LZMAFile(io.BytesIO(data))
        if magic == "zip":
            raise _SourceError("a zip archive cannot be read as tar")
        if magic == "none":
            return io.BytesIO(data)
        raise _SourceError(f"unsupported archive compression for type {source_type}")

    def _decompression_limit(self, stats: _ArchiveStats) -> int:
        remaining_bytes = max(0, self._limits.extracted_max_bytes - self._total_bytes)
        remaining_files = max(0, self._limits.max_files - len(self._file_index))
        return remaining_bytes + remaining_files * 2048 + 8 * 1024 * 1024

    def _count_entry(self, stats: _ArchiveStats) -> None:
        stats.entries += 1
        if len(self._file_index) + stats.entries > self._limits.max_files:
            raise _SourceError("archive file count exceeds the configured limit")

    def _stage_directory(self, stage: Path, relpath: str) -> None:
        target = self._safe_target(stage, relpath)
        if target.exists() and not target.is_dir():
            raise _SourceError(f"archive directory conflicts with a file: {relpath}")
        target.mkdir(exist_ok=True)

    def _stage_symlink(
        self,
        stage: Path,
        relpath: str,
        target: str,
    ) -> None:
        link = self._safe_target(stage, relpath, allow_final_symlink=True)
        os.symlink(target, link)

    def _stage_hardlink(
        self,
        stage: Path,
        relpath: str,
        target: str,
    ) -> None:
        source = self._safe_target(stage, target)
        try:
            info = source.lstat()
        except OSError as err:
            raise _SourceError(f"archive hardlink target is missing: {target}") from err
        if not stat.S_ISREG(info.st_mode):
            raise _SourceError(f"unsafe archive hardlink target: {target}")
        link = self._safe_target(stage, relpath, allow_final_symlink=True)
        os.link(source, link)

    def _stage_regular(
        self,
        stage: Path,
        relpath: str,
        handle: Any,
        expected_size: int,
        stats: _ArchiveStats,
    ) -> None:
        if expected_size < 0:
            raise _SourceError(f"archive member has an invalid size: {relpath}")
        remaining = self._limits.extracted_max_bytes - self._total_bytes
        if stats.bytes + expected_size > remaining:
            raise _SourceError("archive content exceeds the configured byte limit")
        target = self._safe_target(stage, relpath)
        actual = 0
        with target.open("wb") as output:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                actual += len(chunk)
                if stats.bytes + actual > remaining:
                    raise _SourceError(
                        "archive content exceeds the configured byte limit"
                    )
                output.write(chunk)
        stats.files += 1
        stats.bytes += actual

    def _merge_stage(
        self,
        stage: Path,
        module: _Module,
        dest: str,
        identity: str,
        kind: str,
    ) -> list[_File]:
        files: list[_File] = []

        def walk(current: Path, prefix: str) -> None:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
                for entry in entries:
                    relpath = f"{prefix}/{entry.name}" if prefix else entry.name
                    if entry.is_symlink():
                        target = os.readlink(entry.path)
                        final_rel = f"{dest}/{relpath}" if dest else relpath
                        safe_target = _safe_link_target(final_rel, target)
                        link_target = posixpath.relpath(
                            safe_target, posixpath.dirname(final_rel) or "."
                        )
                        link = self._safe_target(
                            module.root,
                            final_rel,
                            allow_final_symlink=True,
                        )
                        os.symlink(link_target, link)
                        old = self._file_index.pop((module.dirname, final_rel), None)
                        if old is not None:
                            self._total_bytes -= old.size
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        walk(Path(entry.path), relpath)
                        continue
                    if entry.is_file(follow_symlinks=False):
                        final_rel = f"{dest}/{relpath}" if dest else relpath
                        files.append(
                            self._install_staged_file(
                                module,
                                final_rel,
                                Path(entry.path),
                                kind,
                                identity,
                            )
                        )
                        continue
                    raise _SourceError(f"unsupported staged archive member: {relpath}")

        walk(stage, "")
        return files

    def _install_staged_file(
        self,
        module: _Module,
        relpath: str,
        staged: Path,
        kind: str,
        source: str,
    ) -> _File:
        size = staged.stat().st_size
        key = (module.dirname, relpath)
        existing = self._file_index.get(key)
        previous = existing.size if existing is not None else 0
        new_total = self._total_bytes - previous + size
        if new_total > self._limits.extracted_max_bytes:
            raise _SourceError("materialized content exceeds the configured byte limit")
        if existing is None and len(self._file_index) >= self._limits.max_files:
            raise _SourceError("materialized file count exceeds the configured limit")
        target = self._safe_target(module.root, relpath, allow_final_symlink=True)
        os.replace(staged, target)
        with target.open("rb") as handle:
            binary = _is_binary(handle.read(8192))
        file = _File(
            module=module.name,
            module_dir=module.dirname,
            relpath=relpath,
            path=target,
            kind=kind,
            source=source,
            size=size,
            binary=binary,
        )
        self._file_index[key] = file
        self._total_bytes = new_total
        return file

    def _git_notices(
        self,
        files: list[_File],
        submodules_disabled: bool,
        lfs_disabled: bool,
    ) -> tuple[list[str], list[str]]:
        problems: list[str] = []
        notices: list[str] = []
        submodules = False
        lfs_paths: list[str] = []
        lfs_attributes = False
        for file in files:
            name = posixpath.basename(file.relpath)
            if name == ".gitmodules":
                submodules = True
            if name == ".gitattributes" and file.size <= 64 * 1024:
                try:
                    text = file.path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                if "filter=lfs" in text:
                    lfs_attributes = True
            if file.size <= 1024 and not file.binary:
                try:
                    data = file.path.read_bytes()
                except OSError:
                    continue
                if _is_lfs_pointer(data):
                    lfs_paths.append(file.relpath)
                    file.source = (
                        f"{file.source}; git LFS pointer, content not materialized"
                    )
        if submodules and not submodules_disabled:
            problems.append(
                "submodule content is not materialized; submodules are never fetched"
            )
        elif submodules:
            notices.append("submodules are disabled by the manifest")
        if lfs_paths:
            shown = ", ".join(lfs_paths[:5])
            if len(lfs_paths) > 5:
                shown += f", and {len(lfs_paths) - 5} more"
            if lfs_disabled:
                notices.append(f"git LFS is disabled by the manifest: {shown}")
            else:
                problems.append(f"git LFS content is not materialized: {shown}")
        elif lfs_attributes:
            notices.append("git LFS attributes are present")
        return problems, notices

    def _make_read_only(self) -> None:
        for current, directories, files in os.walk(
            self._root, topdown=False, followlinks=False
        ):
            root = Path(current)
            for name in files:
                path = root / name
                if path.is_symlink():
                    continue
                with suppress(OSError):
                    os.chmod(path, 0o444)
            for name in directories:
                path = root / name
                if path.is_symlink():
                    continue
                with suppress(OSError):
                    os.chmod(path, 0o555)
        with suppress(OSError):
            os.chmod(self._root, 0o555)

    def _make_writable(self) -> None:
        with suppress(OSError):
            os.chmod(self._root, 0o700)
        for current, directories, files in os.walk(
            self._root, topdown=True, followlinks=False
        ):
            root = Path(current)
            with suppress(OSError):
                os.chmod(root, 0o700)
            for name in directories:
                path = root / name
                if path.is_symlink():
                    continue
                with suppress(OSError):
                    os.chmod(path, 0o700)
            for name in files:
                path = root / name
                if path.is_symlink():
                    continue
                with suppress(OSError):
                    os.chmod(path, 0o600)
