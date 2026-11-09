import base64
import binascii
import json
import os
from typing import Any

import gi
import structlog
import yaml

gi.require_version("Json", "1.0")
from gi.repository import GLib, Json

from app.utils.github import GitHubAPIClient

logger = structlog.get_logger(__name__)

MANIFEST_PRECEDENCE = {".json": 0, ".yaml": 1, ".yml": 2}


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

    parser = Json.Parser()
    try:
        parser.load_from_data(content, -1)
    except GLib.Error as err:
        logger.error(
            "Failed to parse JSON manifest", filename=filename, error=err.message
        )
        return {}

    root = parser.get_root()
    if root is None:
        return {}

    try:
        result = json.loads(Json.to_string(root, False))
    except ValueError as err:
        logger.error(
            "Failed to convert JSON manifest", filename=filename, error=str(err)
        )
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
