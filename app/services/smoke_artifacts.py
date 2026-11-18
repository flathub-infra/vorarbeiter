"""Read bounded application-check artifacts and expose only listed PNG previews."""

import json
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
    validate_source_identity,
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


def result_summary(verify: str, arch: str) -> dict:
    return {
        "verify": verify,
        "screenshots": "skipped",
        "messages": [],
        "previews": [],
        "arch": arch,
        "unexecuted_modes": [],
        "missing_inputs": False,
    }


def _object(data: bytes) -> dict:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise TypeError("expected a JSON object in smoke results")
    return value


def _validate_cli_result(result: dict, app_ref: str) -> None:
    if type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        raise ValueError("unsupported CLI result schema")
    required = {"status", "app_ref", "artifact", "timings_ms", "screenshots", "failure"}
    if not required <= result.keys():
        raise ValueError("missing required CLI result fields")
    status, failure = result["status"], result["failure"]
    if status not in ("passed", "failed"):
        raise ValueError("invalid CLI result status")
    if result["app_ref"] != app_ref and not (
        status == "failed" and result["app_ref"] is None
    ):
        raise ValueError("CLI app ref differs from the requested app ref")
    if status == "passed":
        if failure is not None:
            raise ValueError("passed CLI result contains a failure")
    elif (
        not isinstance(failure, dict)
        or not isinstance(failure.get("reason"), str)
        or not failure["reason"]
        or not isinstance(failure.get("message"), str)
    ):
        raise ValueError("failed CLI result lacks failure details")
    if not isinstance(result["screenshots"], list):
        raise TypeError("invalid CLI screenshots")
    for path in result["screenshots"]:
        relative_path(path)
    artifact = result["artifact"]
    if (
        not isinstance(artifact, dict)
        or artifact.get("kind") not in ("repo", "bundle")
        or not isinstance(artifact.get("path"), str)
    ):
        raise ValueError("invalid CLI artifact")
    timings = result["timings_ms"]
    if (
        not isinstance(timings, dict)
        or type(timings.get("total")) is not int
        or timings["total"] < 0
    ):
        raise ValueError("invalid CLI total timing")
    for name in ("install", "launch_to_window"):
        value = timings.get(name)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("invalid CLI phase timing")


def _capture_captions(data: bytes) -> dict[str, str]:
    manifest = _object(data)
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
    ):
        raise ValueError("unknown screenshot manifest schema")
    if not isinstance(manifest.get("captures"), list):
        raise TypeError("invalid screenshot manifest")
    captions = {}
    for capture in manifest["captures"][:100]:
        if not isinstance(capture, dict) or not isinstance(capture.get("path"), str):
            raise TypeError("invalid screenshot capture")
        captions[capture["path"]] = str(capture.get("caption", "Screenshot"))[:200]
    return captions


def _apply_execution(data: bytes, mode: str, has_result: bool, summary: dict) -> None:
    execution = _object(data)
    if (
        type(execution.get("schema_version")) is not int
        or execution["schema_version"] != 1
    ):
        raise ValueError("unknown smoke execution schema")
    wrapper_status = execution.get("status")
    if wrapper_status != "completed":
        summary["messages"].append(f"{mode}: wrapper {str(wrapper_status)[:100]}")
        # Keep a CLI failure's more specific verdict and diagnostic.
        if not has_result or summary[mode] == "passed":
            summary[mode] = (
                "cancelled" if wrapper_status == "cancelled" else "infrastructure_error"
            )
    elif execution.get("exit_code") != 0 and summary[mode] == "passed":
        summary[mode] = "infrastructure_error"
    cleanup = execution.get("cleanup") or {}
    if not isinstance(cleanup, dict):
        raise TypeError("invalid smoke cleanup record")
    for stage, detail in cleanup.items():
        if not isinstance(detail, dict):
            raise TypeError("invalid smoke cleanup stage")
        if detail.get("status") not in {"ok", "absent"}:
            summary["messages"].append(
                f"{mode}: container cleanup {str(stage)[:100]} {str(detail.get('status'))[:100]}"
            )
            if summary[mode] == "passed":
                summary[mode] = "infrastructure_error"


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
    summary = result_summary("skipped", arch)
    summary["missing_inputs"] = request.get("missing_inputs") is True
    if request.get("state") != "ready":
        state = request.get("state")
        if state not in {"skipped", "configuration_error", "infrastructure_error"}:
            raise ValueError("invalid smoke request state")
        summary["verify"] = state
        summary["messages"].append(str(request.get("message", ""))[:1000])
        return SmokeArtifact(summary, {})

    images = {}
    sha, app_ref = request.get("sha", ""), request.get("app_ref", "")
    validate_source_identity(sha, app_ref)
    for mode in ("screenshots", "verify"):
        if mode == "screenshots" and request.get("recipe_error"):
            summary[mode] = "configuration_error"
            summary["messages"].append(
                f"screenshots: {str(request['recipe_error'])[:1000]}"
            )
            continue
        if mode == "screenshots" and not request.get("recipe"):
            continue
        outcome = report.get("outcomes", {}).get(mode)
        result_bytes = files.get(f"{mode}/result.json")
        result = None
        result_error = None
        if result_bytes and outcome != "cancelled":
            try:
                result = _object(result_bytes)
                _validate_cli_result(result, app_ref)
            except (ValueError, TypeError) as error:
                result_error = str(error)
        if outcome == "cancelled":
            summary[mode] = "cancelled"
        elif not result_bytes:
            summary[mode] = "infrastructure_error"
            summary["messages"].append(f"{mode}: no result was produced")
            if outcome == "skipped" and f"{mode}.execution.json" not in files:
                summary["unexecuted_modes"].append(mode)
        elif result_error is not None:
            summary[mode] = "infrastructure_error"
            summary["messages"].append(f"{mode}: invalid result: {result_error}")
        else:
            assert result is not None
            status = result["status"]
            summary[mode] = (
                status
                if status == "failed" or outcome == "success"
                else "infrastructure_error"
            )
            failure = result["failure"]
            if failure:
                reason = failure.get("reason")
                if (
                    reason
                    in {
                        "install_failed",
                        "dependency_failed",
                        "display_start_failed",
                        "display_exited",
                        "internal_error",
                    }
                    or failure.get("category") == "infrastructure"
                ):
                    summary[mode] = "infrastructure_error"
                elif (
                    reason == "screenshot_failed"
                    and failure.get("category") != "expectation"
                ):
                    summary[mode] = "infrastructure_error"
                    summary["messages"].append(
                        f"{mode}: unclassified capture failure; app/recipe fault is not established"
                    )
                summary["messages"].append(
                    f"{mode}: {str(failure.get('message', reason))[:1000]}"
                )
            captions = {}
            if mode == "screenshots" and "screenshots/screenshots.json" in files:
                try:
                    captions = _capture_captions(files["screenshots/screenshots.json"])
                except (ValueError, TypeError) as error:
                    summary[mode] = "infrastructure_error"
                    summary["messages"].append(f"{mode}: invalid manifest: {error}")
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
                    summary[mode] = "infrastructure_error"
                    summary["messages"].append(f"{mode}: preview is not a PNG")
                    continue
                width, height = struct.unpack(">II", content[16:24])
                if not 0 < width <= 8192 or not 0 < height <= 8192:
                    summary[mode] = "infrastructure_error"
                    summary["messages"].append(f"{mode}: invalid PNG dimensions")
                    continue
                if len(images) < 3 and path not in images:
                    caption = captions.get(
                        name,
                        "Screenshot diagnostic"
                        if name.startswith("logs/")
                        else "Startup screenshot",
                    )
                    summary["previews"].append({"path": path, "caption": caption})
                    images[path] = content
        execution_bytes = files.get(f"{mode}.execution.json")
        if execution_bytes:
            try:
                _apply_execution(execution_bytes, mode, bool(result_bytes), summary)
            except (ValueError, TypeError) as error:
                summary[mode] = "infrastructure_error"
                summary["messages"].append(f"{mode}: invalid execution record: {error}")
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
