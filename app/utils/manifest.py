import base64
import json
import os
import re
from typing import Any

import gi
import structlog
import yaml

gi.require_version("Json", "1.0")
from gi.repository import GLib, Json  # noqa: E402  # ty: ignore[unresolved-import]

from app.utils.github import GitHubAPIClient  # noqa: E402

logger = structlog.get_logger(__name__)

MANIFEST_EXTENSIONS = (".yml", ".yaml", ".json")

# json-glib's non-strict mode handles /* */ block comments and single-quoted
# strings, but not // line comments or trailing commas — strip those first.
_JSON_LINE_COMMENT_PATTERN = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*')
_JSON_TRAILING_COMMA_PATTERN = re.compile(r",(\s*[}\]])")


def _preprocess_json(content: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return match.group(1) if match.group(1) is not None else ""

    stripped = _JSON_LINE_COMMENT_PATTERN.sub(replace, content)
    return _JSON_TRAILING_COMMA_PATTERN.sub(r"\1", stripped)


def _parse_manifest(filename: str, content: str) -> dict[str, Any]:
    if filename.endswith((".yml", ".yaml")):
        try:
            result = yaml.safe_load(content)
            return result if isinstance(result, dict) else {}
        except yaml.YAMLError as err:
            logger.error(
                "Failed to parse YAML manifest", filename=filename, error=str(err)
            )
            return {}

    parser = Json.Parser()
    try:
        parser.load_from_data(_preprocess_json(content), -1)
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
) -> tuple[str | None, str | None]:
    """Detect Flatpak app ID by reading manifests via GitHub Contents API.

    Returns (manifest_filename, appid) or (None, None).
    """
    response = await client.request(
        "get",
        f"https://api.github.com/repos/{fork_repo}/contents/?ref={ref}",
        context={"fork_repo": fork_repo, "ref": ref},
    )
    if not response:
        logger.error("Failed to list repo contents", fork_repo=fork_repo, ref=ref)
        return None, None

    entries = response.json()
    if not isinstance(entries, list):
        logger.error("Unexpected contents response", fork_repo=fork_repo, ref=ref)
        return None, None

    manifest_files = [
        entry["name"]
        for entry in entries
        if entry.get("type") == "file" and entry["name"].endswith(MANIFEST_EXTENSIONS)
    ]

    if not manifest_files:
        logger.error("No manifest files found", fork_repo=fork_repo, ref=ref)
        return None, None

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
        except Exception:
            logger.error("Failed to decode file content", filename=filename)
            continue

        manifest = _parse_manifest(filename, content)
        appid = _get_appid_from_manifest(manifest)

        if appid and os.path.splitext(filename)[0] == appid:
            logger.info("Detected Flatpak ID", appid=appid, manifest_file=filename)
            return filename, appid

    return None, None
