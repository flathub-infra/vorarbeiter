import datetime
import re
import time
import xml.etree.ElementTree as ET
import zlib
from collections import OrderedDict
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2 as httpx
from markupsafe import Markup, escape


class PreviewUnavailable(Exception):
    pass


class PreviewError(Exception):
    pass


_MAX_INPUT = 1024 * 1024
_CACHE_TTL = 600
_CACHE_SIZE = 100
_cache: OrderedDict[tuple[int, str], tuple[float, dict[str, Any]]] = OrderedDict()
_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_ALLOWED = {"p", "ul", "ol", "li", "em", "code"}
_LINKS = (
    ("homepage", "Project Website"),
    ("contact", "Contact"),
    ("bugtracker", "Report an Issue"),
    ("vcs-browser", "Browse the source code"),
    ("translate", "Help with translations"),
)


class _TreeBuilder(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise PreviewError("AppStream metadata contains a DTD")


def _children(parent: ET.Element, path: str) -> list[ET.Element]:
    nodes = [parent] if _LANG not in parent.attrib else []
    for tag in path.split("/"):
        nodes = [
            child
            for node in nodes
            for child in node
            if child.tag == tag and _LANG not in child.attrib
        ]
    return nodes


def _text(parent: ET.Element, path: str) -> str | None:
    for node in _children(parent, path):
        if node.text and node.text.strip():
            return node.text.strip()
    return None


def _markup(node: ET.Element) -> Markup:
    if _LANG in node.attrib:
        return Markup("")
    inner = escape(node.text or "")
    for child in node:
        inner += _markup(child) + escape(child.tail or "")
    if node.tag in _ALLOWED:
        return Markup("<{}>{}</{}>").format(node.tag, inner, node.tag)
    return escape(f"<{node.tag}>") + inner + escape(f"</{node.tag}>")


def _description(parent: ET.Element) -> Markup:
    descriptions = _children(parent, "description")
    if not descriptions:
        return Markup("")
    node = descriptions[0]
    result = escape(node.text or "")
    for child in node:
        result += _markup(child) + escape(child.tail or "")
    return result


def _https_url(value: str | None) -> str | None:
    if (
        not value
        or "\\" in value
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            return None
        if parts.port is not None and not 1 <= parts.port <= 65535:
            return None
    except ValueError:
        return None
    return value


def _license(component: ET.Element) -> tuple[str | None, str | None]:
    value = _text(component, "project_license")
    if not value:
        return None, None
    if value != "LicenseRef-proprietary" and not value.startswith(
        "LicenseRef-proprietary="
    ):
        return value, None
    return "Proprietary", _https_url(value.partition("=")[2])


def _gallery_style(component: ET.Element) -> str:
    colors = {"light": "#e0dfe2", "dark": "#403a49"}
    for color in _children(component, "branding/color"):
        scheme = color.get("scheme_preference")
        value = (color.text or "").strip()
        if scheme in colors and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            colors[scheme] = value
    style = ""
    for scheme, color in colors.items():
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        luminance = sum(
            weight
            * (value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
            for weight, value in zip((0.2126, 0.7152, 0.0722), channels)
        )
        foreground = "#15141b" if luminance > 0.179 else "#ffffff"
        style += f"--gallery-{scheme}:{color};--gallery-text-{scheme}:{foreground};"
    return style


def _icon_url(component: ET.Element, base: str) -> str | None:
    candidates = []
    for icon in _children(component, "icon"):
        if icon.get("type") != "cached":
            continue
        width, height = icon.get("width"), icon.get("height")
        if width != height or width not in {"48", "64", "128", "256", "512"}:
            continue
        name = (icon.text or "").strip()
        if (
            not name.lower().endswith(".png")
            or any(c in name for c in "/\\")
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
        ):
            continue
        candidates.append((int(width), name))
    if not candidates:
        return None
    size, name = max(candidates)
    return f"{base}/icons/{size}x{size}/{quote(name, safe='')}"


def _context(component: ET.Element, build_id: int, arch: str) -> dict[str, Any]:
    name = _text(component, "name") or _text(component, "id") or ""
    urls = {}
    for node in _children(component, "url"):
        url = _https_url((node.text or "").strip())
        if url and node.get("type"):
            urls[node.get("type")] = url
    homepage = urls.get("homepage")
    shots = []
    for screenshot in _children(component, "screenshots/screenshot")[:20]:
        for image in _children(screenshot, "image"):
            if image.get("type") == "source":
                source = _https_url((image.text or "").strip())
                if source:
                    shots.append(
                        {
                            "caption": _text(screenshot, "caption") or name or "",
                            "source": source,
                        }
                    )
                    break
    release = None
    for node in _children(component, "releases/release"):
        if node.get("type", "stable") != "stable":
            continue
        date = node.get("date", "")
        raw_timestamp = node.get("timestamp")
        if raw_timestamp:
            try:
                timestamp = datetime.datetime.fromtimestamp(
                    int(raw_timestamp), datetime.UTC
                )
                date = f"{timestamp.strftime('%B')} {timestamp.day}, {timestamp.year}"
            except (ValueError, OverflowError, OSError):
                pass
        notes = _description(node)
        if not _children(node, "description"):
            notes = Markup("<p>No changelog provided.</p>")
        release = {"version": node.get("version", ""), "date": date, "notes": notes}
        break
    base = f"https://dl.flathub.org/build-repo/{build_id}/appstream/{arch}"
    license, license_url = _license(component)
    return {
        "arch": arch,
        "name": name,
        "summary": _text(component, "summary") or "",
        "developer": _text(component, "developer/name")
        or _text(component, "developer_name"),
        "app_id": _text(component, "id"),
        "license": license,
        "license_url": license_url,
        "category": _text(component, "categories/category"),
        "homepage": homepage,
        "homepage_host": urlsplit(homepage).netloc if homepage else None,
        "description": _description(component),
        "gallery_style": _gallery_style(component),
        "shots": shots,
        "release": release,
        "links": [
            {"key": key, "label": label, "url": urls[key]}
            for key, label in _LINKS
            if key in urls
        ],
        "icon_url": _icon_url(component, base),
    }


def _parse(data: bytes, app_id: str) -> ET.Element | None:
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        xml = decoder.decompress(data, _MAX_INPUT + 1)
        if len(xml) > _MAX_INPUT or decoder.unconsumed_tail:
            raise PreviewError("AppStream metadata is too large")
        if not decoder.eof or decoder.unused_data:
            raise PreviewError("Invalid compressed AppStream metadata")
        root = ET.fromstring(xml, parser=ET.XMLParser(target=_TreeBuilder()))
    except (zlib.error, ET.ParseError, ValueError, LookupError) as exc:
        raise PreviewError("Invalid AppStream metadata") from exc
    if root.tag != "components":
        raise PreviewError("Invalid AppStream metadata")
    for component in root:
        if component.tag == "component" and _text(component, "id") in (
            app_id,
            f"{app_id}.desktop",
        ):
            return component
    return None


async def fetch_preview(build_id: int, app_id: str) -> dict[str, Any]:
    if not isinstance(build_id, int) or isinstance(build_id, bool) or build_id < 0:
        raise PreviewError("Invalid build ID")
    key = (build_id, app_id)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached:
        if now - cached[0] < _CACHE_TTL:
            _cache.move_to_end(key)
            return cached[1]
        del _cache[key]
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            for arch in ("x86_64", "aarch64"):
                url = f"https://dl.flathub.org/build-repo/{build_id}/appstream/{arch}/appstream.xml.gz"
                async with client.stream(
                    "GET", url, headers={"Accept-Encoding": "identity"}
                ) as response:
                    if response.status_code == 404:
                        continue
                    if response.status_code != 200:
                        raise PreviewError(
                            f"AppStream upstream returned HTTP {response.status_code}"
                        )
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(data) + len(chunk) > _MAX_INPUT:
                            raise PreviewError(
                                "Compressed AppStream metadata is too large"
                            )
                        data.extend(chunk)
                component = _parse(bytes(data), app_id)
                if component is None:
                    continue
                try:
                    context = _context(component, build_id, arch)
                except RecursionError as exc:
                    raise PreviewError(
                        "AppStream metadata is too deeply nested"
                    ) from exc
                _cache[key] = (time.monotonic(), context)
                _cache.move_to_end(key)
                while len(_cache) > _CACHE_SIZE:
                    _cache.popitem(last=False)
                return context
    except httpx.HTTPError as exc:
        raise PreviewError("AppStream upstream request failed") from exc
    raise PreviewUnavailable("AppStream metadata or component is unavailable")
