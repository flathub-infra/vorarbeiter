"""Read bounded application-check artifacts and expose only listed PNG previews."""

import json
import re
import stat
import struct
import time
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlparse

import httpx2 as httpx

from app.smoke import (
    MAX_ENTRIES,
    MAX_JSON,
    MAX_PNG,
    MAX_RESULT,
    PNG_SIGNATURE,
    relative_path,
)
from app.utils.github import get_github_actions_client

REPOSITORY = "flathub-infra/vorarbeiter"
API = f"https://api.github.com/repos/{REPOSITORY}"


@dataclass
class SmokeArtifact:
    summary: dict
    images: dict[str, bytes]
    source_sha: str | None = None
    app_ref: str | None = None


def _object(data: bytes) -> dict:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object in smoke results")
    return value


def parse_result_archive(data: bytes) -> SmokeArtifact:
    """Validate ZIP member limits before interpreting the runner's result contract."""
    if len(data) > MAX_RESULT:
        raise ValueError("smoke archive exceeds size limit")
    files = {}
    with zipfile.ZipFile(BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_RESULT:
            raise ValueError("smoke archive exceeds expanded limits")
        names = set()
        for info in infos:
            name = relative_path(info.filename.rstrip("/"))
            if name in names or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("duplicate or linked smoke archive member")
            names.add(name)
            if info.is_dir():
                continue
            limit = MAX_JSON if name.endswith(".json") else MAX_PNG
            if info.file_size > limit:
                raise ValueError("oversized smoke archive member")
            with archive.open(info) as stream:
                content = stream.read(limit + 1)
            if len(content) > limit:
                raise ValueError("oversized smoke archive member")
            files[name] = content

    report = _object(files["report.json"])
    if report.get("schema_version") != 1:
        raise ValueError("unknown smoke report schema")
    request = report["request"]
    if not isinstance(request, dict) or not isinstance(
        report.get("outcomes", {}), dict
    ):
        raise TypeError("invalid smoke report objects")
    arch = request.get("arch", "x86_64")
    if arch not in {"x86_64", "aarch64"}:
        raise ValueError("unknown smoke architecture")
    summary = {
        "verify": "skipped",
        "screenshots": "skipped",
        "messages": [],
        "previews": [],
        "arch": arch,
    }
    if request.get("state") != "ready":
        state = request.get("state")
        if state not in {"skipped", "configuration_error", "infrastructure_error"}:
            raise ValueError("invalid smoke request state")
        summary["verify"] = state
        summary["messages"].append(str(request.get("message", ""))[:1000])
        return SmokeArtifact(summary, {})

    images = {}
    sha, app_ref = request.get("sha", ""), request.get("app_ref", "")
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or not re.fullmatch(
        r"app/[A-Za-z0-9_.-]+/x86_64/[A-Za-z0-9_.-]+", app_ref
    ):
        raise ValueError("missing or invalid tested source identity")
    for mode in ("screenshots", "verify"):
        if mode == "screenshots" and not request.get("recipe"):
            continue
        outcome = report.get("outcomes", {}).get(mode)
        result_bytes = files.get(f"{mode}/result.json")
        if outcome == "cancelled":
            summary[mode] = "cancelled"
        elif not result_bytes:
            summary[mode] = "infrastructure_error"
            summary["messages"].append(f"{mode}: no result was produced")
        else:
            result = _object(result_bytes)
            status = result.get("status")
            if status not in {"passed", "failed"}:
                raise ValueError("invalid smoke result status")
            summary[mode] = (
                status
                if status == "failed" or outcome == "success"
                else "infrastructure_error"
            )
            failure = result.get("failure") or {}
            if not isinstance(failure, dict) or not isinstance(
                result.get("screenshots", []), list
            ):
                raise ValueError("invalid smoke result structure")
            if failure:
                reason = failure.get("reason")
                if reason in {
                    "dependency_failed",
                    "display_start_failed",
                    "internal_error",
                }:
                    summary[mode] = "infrastructure_error"
                summary["messages"].append(
                    f"{mode}: {str(failure.get('message', reason))[:1000]}"
                )
            captions = {}
            if mode == "screenshots" and "screenshots/screenshots.json" in files:
                manifest = _object(files["screenshots/screenshots.json"])
                if not isinstance(manifest.get("captures", []), list):
                    raise ValueError("invalid screenshot manifest")
                for capture in manifest.get("captures", [])[:100]:
                    if not isinstance(capture, dict) or not isinstance(
                        capture.get("path"), str
                    ):
                        raise TypeError("invalid screenshot capture")
                    captions[capture["path"]] = str(
                        capture.get("caption", "Screenshot")
                    )[:200]
            for name in result.get("screenshots", [])[:100]:
                path = f"{mode}/{relative_path(name)}"
                content = files.get(path)
                if content is None:
                    continue
                if (
                    len(content) < 24
                    or not content.startswith(PNG_SIGNATURE)
                    or content[12:16] != b"IHDR"
                ):
                    raise ValueError("preview is not a PNG")
                width, height = struct.unpack(">II", content[16:24])
                if not 0 < width <= 8192 or not 0 < height <= 8192:
                    raise ValueError("invalid PNG dimensions")
                if len(images) < 3 and path not in images:
                    caption = captions.get(
                        name,
                        "Screenshot diagnostic"
                        if name.startswith("logs/")
                        else "Startup screenshot",
                    )
                    summary["previews"].append({"path": path, "caption": caption})
                    images[path] = content
    return SmokeArtifact(summary, images, sha, app_ref)


# Store only parsed summaries and at most three small PNGs per artifact.
_cache: OrderedDict[int, tuple[float, SmokeArtifact]] = OrderedDict()
MAX_CACHE_BYTES = 64 * 1024**2


def _cache_result(artifact_id: int, result: SmokeArtifact) -> None:
    _cache[artifact_id] = (time.monotonic() + 600, result)
    _cache.move_to_end(artifact_id)
    while (
        len(_cache) > 20
        or sum(len(image) for _, r in _cache.values() for image in r.images.values())
        > MAX_CACHE_BYTES
    ):
        _cache.popitem(last=False)


async def run_artifacts(run_id: int) -> list[dict]:
    client = get_github_actions_client()
    artifacts = []
    for page in range(1, 11):
        response = await client.request(
            "get",
            f"{API}/actions/runs/{run_id}/artifacts",
            params={"per_page": 100, "page": page},
        )
        if response is None:
            raise RuntimeError("could not list smoke artifacts")
        batch = response.json()["artifacts"]
        artifacts.extend(batch)
        if len(batch) < 100:
            return artifacts
    raise ValueError("too many smoke artifacts")


async def download_artifact(artifact: dict) -> SmokeArtifact:
    """Follow the API's signed storage URL anonymously, with a streaming byte cap."""
    if artifact.get("expired"):
        raise FileNotFoundError("smoke artifact expired")
    if artifact.get("size_in_bytes", MAX_RESULT + 1) > MAX_RESULT:
        raise ValueError("smoke artifact exceeds size limit")
    artifact_id = int(artifact["id"])
    if artifact_id in _cache:
        expires, cached = _cache.pop(artifact_id)
        if expires > time.monotonic():
            _cache[artifact_id] = (expires, cached)
            return cached
    response = await get_github_actions_client().request(
        "get",
        f"{API}/actions/artifacts/{artifact_id}/zip",
        follow_redirects=False,
        raise_for_status=False,
    )
    if response is None:
        raise RuntimeError("GitHub artifact request temporarily failed")
    if response.status_code in {404, 410}:
        raise FileNotFoundError("smoke artifact is unavailable")
    if response.status_code != 302:
        raise RuntimeError(f"GitHub artifact request returned {response.status_code}")
    url = response.headers.get("location", "")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not parsed.hostname
        or not parsed.hostname.endswith(
            (".blob.core.windows.net", ".githubusercontent.com")
        )
    ):
        raise ValueError("unexpected GitHub artifact download host")
    async with (
        httpx.AsyncClient(timeout=30, follow_redirects=False) as client,
        client.stream("GET", url) as download,
    ):
        download.raise_for_status()
        data = bytearray()
        async for block in download.aiter_bytes():
            data.extend(block)
            if len(data) > MAX_RESULT:
                raise ValueError("smoke artifact exceeds size limit")
    result = parse_result_archive(bytes(data))
    _cache_result(artifact_id, result)
    return result
