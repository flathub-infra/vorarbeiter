import base64
import binascii
import json
import os
import posixpath
import re
from typing import Any
from urllib.parse import quote

import structlog
import yaml

from app.utils.github import GitHubAPIClient

logger = structlog.get_logger(__name__)

MANIFEST_PRECEDENCE = {".json": 0, ".yaml": 1, ".yml": 2}
_RELAXED_JSON_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\'([^\']*)\'|/\*.*?\*/', re.DOTALL)


def _relaxed_json_token_to_json(match: re.Match[str]) -> str:
    token = match.group(0)
    if token.startswith('"'):
        return token
    if token.startswith("'"):
        return json.dumps(match.group(1))
    return " "


def _yaml_scalar_text(content: str, key: str) -> str | None:
    try:
        node = yaml.compose(content)
    except yaml.YAMLError:
        return None
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value_node.value if isinstance(value_node, yaml.ScalarNode) else None
    return None


def _parse_manifest(filename: str, content: str) -> dict[str, Any]:
    if filename.endswith((".yml", ".yaml")):
        try:
            result = yaml.safe_load(content)
            if not isinstance(result, dict):
                return {}
            branch = _yaml_scalar_text(content, "branch")
            if branch is not None:
                result["branch"] = branch
            return result
        except yaml.YAMLError as err:
            logger.error(
                "Failed to parse YAML manifest", filename=filename, error=str(err)
            )
            return {}

    try:
        result = json.loads(
            _RELAXED_JSON_TOKEN.sub(
                _relaxed_json_token_to_json, content.removeprefix("\ufeff")
            )
        )
    except ValueError as err:
        logger.error("Failed to parse JSON manifest", filename=filename, error=str(err))
        return {}

    return result if isinstance(result, dict) else {}


def _get_appid_from_manifest(manifest: dict[str, Any]) -> str | None:
    return manifest.get("app-id") or manifest.get("id")


async def detect_appid_from_github(
    client: GitHubAPIClient,
    fork_repo: str,
    ref: str,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Detect Flatpak app ID by reading manifests via GitHub Contents API.

    Returns (manifest_filename, appid, manifest) or (None, None, {}).
    """
    response = await client.request(
        "get",
        f"https://api.github.com/repos/{fork_repo}/contents/?ref={ref}",
        context={"fork_repo": fork_repo, "ref": ref},
    )
    if not response:
        logger.error("Failed to list repo contents", fork_repo=fork_repo, ref=ref)
        return None, None, {}

    entries = response.json()
    if not isinstance(entries, list):
        logger.error("Unexpected contents response", fork_repo=fork_repo, ref=ref)
        return None, None, {}

    manifest_files = sorted(
        (
            entry["name"]
            for entry in entries
            if entry.get("type") == "file"
            and os.path.splitext(entry["name"])[1] in MANIFEST_PRECEDENCE
        ),
        key=lambda name: MANIFEST_PRECEDENCE[os.path.splitext(name)[1]],
    )

    if not manifest_files:
        logger.error("No manifest files found", fork_repo=fork_repo, ref=ref)
        return None, None, {}

    for filename in manifest_files:
        logger.info("Checking file for Flatpak ID", filename=filename)

        file_response = await client.request(
            "get",
            f"https://api.github.com/repos/{fork_repo}/contents/{filename}?ref={ref}",
            context={"fork_repo": fork_repo, "ref": ref, "filename": filename},
        )
        if not file_response:
            continue

        file_data = file_response.json()
        content_b64 = file_data.get("content", "")
        try:
            content = base64.b64decode(content_b64).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            logger.error("Failed to decode file content", filename=filename)
            continue

        manifest = _parse_manifest(filename, content)
        appid = _get_appid_from_manifest(manifest)

        if appid and os.path.splitext(filename)[0] == appid:
            logger.info("Detected Flatpak ID", appid=appid, manifest_file=filename)
            return filename, appid, manifest

    return None, None, {}


class _Unresolvable(Exception):
    pass


MAX_MANIFEST_INCLUDES = 50
SYMLINK_MODE = "120000"
SUBMODULE_TYPE = "commit"
RESERVED_BUILD_PATHS = (
    "repo",
    "builddir",
    ".flatpak-builder",
    ".flathub.justfile",
    "tmp",
    ".local",
    "app_id",
    ".ccache-marker",
)


def _load_manifest_data(path: str, content: str) -> Any:
    if path.endswith((".yml", ".yaml")):
        return yaml.safe_load(content)
    return json.loads(
        _RELAXED_JSON_TOKEN.sub(
            _relaxed_json_token_to_json, content.removeprefix("\ufeff")
        )
    )


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Unresolvable(f"expected a list, got {type(value).__name__}")
    return value


async def manifest_read_paths(
    client: GitHubAPIClient, repo: str, sha: str, app_id: str
) -> set[str] | None:
    read = {
        "flathub.json",
        ".gitmodules",
        *RESERVED_BUILD_PATHS,
    }
    fetched: set[str] = set()

    async def get(url: str, raw: bool, context: dict[str, Any]) -> Any:
        response = await client.request(
            "get",
            url,
            headers={"Accept": "application/vnd.github.raw+json"} if raw else {},
            context={"repo": repo, "sha": sha, **context},
        )
        if response is None:
            raise _Unresolvable(f"could not fetch {url}")
        return response.text if raw else response.json()

    def resolve(base: str, path: Any) -> str:
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise _Unresolvable(f"unsupported path {path!r}")
        resolved = posixpath.normpath(posixpath.join(base, path))
        if resolved in (".", "..") or resolved.startswith("../"):
            raise _Unresolvable(f"path {path!r} is not inside the repository")
        parts = resolved.split("/")
        if any(
            "/".join(parts[:depth]) in symlinks for depth in range(1, len(parts) + 1)
        ):
            raise _Unresolvable(f"path {resolved} goes through a symlink")
        read.add(resolved)
        return resolved

    def in_submodule(path: str) -> bool:
        return any(
            path == submodule or path.startswith(f"{submodule}/")
            for submodule in submodules
        )

    async def include(base: str, path: str) -> tuple[Any, str] | None:
        resolved = resolve(base, path)
        if resolved in fetched or in_submodule(resolved):
            return None
        if resolved not in blobs:
            raise _Unresolvable(f"{resolved} is not a file")
        if len(fetched) >= MAX_MANIFEST_INCLUDES:
            raise _Unresolvable("too many included files")
        fetched.add(resolved)
        content = await get(
            f"https://api.github.com/repos/{repo}/contents/{quote(resolved)}?ref={sha}",
            raw=True,
            context={"path": resolved},
        )
        return _load_manifest_data(resolved, content), posixpath.dirname(resolved)

    async def modules(value: Any, base: str) -> None:
        for item in _items(value):
            item_base = base
            if isinstance(item, str):
                loaded = await include(base, item)
                if loaded is None:
                    continue
                item, item_base = loaded
            if not isinstance(item, dict):
                raise _Unresolvable("module is not an object")
            await sources(item.get("sources"), item_base)
            await modules(item.get("modules"), item_base)

    async def sources(value: Any, base: str) -> None:
        for item in _items(value):
            if isinstance(item, str):
                loaded = await include(base, item)
                if loaded is not None:
                    data, data_base = loaded
                    await sources(data if isinstance(data, list) else [data], data_base)
                continue
            if not isinstance(item, dict):
                raise _Unresolvable("source is not an object")
            if "path" in item:
                resolve(base, item["path"])
            for path in _items(item.get("paths")):
                resolve(base, path)

    try:
        tree = await get(
            f"https://api.github.com/repos/{repo}/git/trees/{sha}?recursive=1",
            raw=False,
            context={},
        )
        if not isinstance(tree, dict) or tree.get("truncated") is not False:
            raise _Unresolvable("repository tree is unavailable or truncated")
        entries = tree.get("tree") or []
        blobs = {e["path"] for e in entries if e.get("type") == "blob"}
        symlinks = {e["path"] for e in entries if e.get("mode") == SYMLINK_MODE}
        submodules = {e["path"] for e in entries if e.get("type") == SUBMODULE_TYPE}
        read |= submodules
        manifest_path = next(
            (
                f"{app_id}{ext}"
                for ext in MANIFEST_PRECEDENCE
                if f"{app_id}{ext}" in blobs
            ),
            None,
        )
        if manifest_path is None:
            raise _Unresolvable("manifest not found")
        loaded = await include("", manifest_path)
        manifest = loaded[0] if loaded else None
        if not isinstance(manifest, dict):
            raise _Unresolvable("manifest is not an object")
        for key in ("metadata", "metadata-platform"):
            if key in manifest:
                resolve("", manifest[key])
        await modules(manifest.get("modules"), "")
    except (_Unresolvable, KeyError, ValueError, yaml.YAMLError) as err:
        logger.info(
            "Could not resolve manifest read paths", repo=repo, sha=sha, error=str(err)
        )
        return None
    return read
