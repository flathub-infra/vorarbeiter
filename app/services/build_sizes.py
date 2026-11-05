"""Best-effort app-ref sizes from the committed build's Flatpak summary.

Flatpak's xa.cache (summary v0) and xa.data (v1) store (installed bytes,
download bytes, metadata), with big-endian uint64 sizes. These are the same
figures used by flatpak_remote_state_lookup_cache, including declared extra
data sizes; they are not the OSTree commit size or a workflow archive size.
"""

import asyncio
import re
import sys
from dataclasses import dataclass
from typing import Literal

import httpx2 as httpx
import structlog
from gi.repository import GLib

logger = structlog.get_logger(__name__)

_MAX_SUMMARY = 2 * 1024 * 1024
_MAX_REFS = 1024
_TIMEOUT = 10
_SUMMARY_TYPE = "(a(s(taya{sv}))a{sv})"


@dataclass(frozen=True)
class BuildSize:
    arch: str
    download: int
    installed: int


def _from_endian(value: int, size: int, byteorder: Literal["big", "little"]) -> int:
    return int.from_bytes(value.to_bytes(size, sys.byteorder), byteorder)


def parse_build_sizes(data: bytes, app_id: str) -> list[BuildSize]:
    if not data or len(data) > _MAX_SUMMARY:
        return []
    summary = GLib.Variant.new_from_bytes(
        GLib.VariantType.new(_SUMMARY_TYPE), GLib.Bytes.new(data), False
    )
    if not summary.is_normal_form():
        return []
    refs = summary.get_child_value(0)
    metadata = summary.get_child_value(1)
    if refs.n_children() > _MAX_REFS:
        return []
    version_value = metadata.lookup_value("xa.summary-version", None)
    version = 0
    if version_value is not None:
        if version_value.get_type_string() != "u":
            return []
        version = _from_endian(version_value.get_uint32(), 4, "little")
    cache = None
    if version == 0:
        wrapped = metadata.lookup_value("xa.cache", GLib.VariantType.new("v"))
        if wrapped is None:
            return []
        cache = wrapped.get_variant()
        if cache.get_type_string() != "a{s(tts)}" or cache.n_children() > _MAX_REFS:
            return []
    elif version != 1:
        return []

    sizes: dict[str, BuildSize] = {}
    seen: set[str] = set()
    ambiguous: set[str] = set()
    for index in range(refs.n_children()):
        entry = refs.get_child_value(index)
        ref = entry.get_child_value(0).get_string()
        parts = ref.split("/")
        if (
            len(parts) != 4
            or parts[:2] != ["app", app_id]
            or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", parts[2])
            or not parts[3]
        ):
            continue
        arch = parts[2]
        # Never pick an arbitrary branch when the repository has several.
        if arch in seen:
            ambiguous.add(arch)
        seen.add(arch)
        if len(seen) > 32:
            return []
        if cache is not None:
            value = cache.lookup_value(ref, GLib.VariantType.new("(tts)"))
        else:
            ref_metadata = entry.get_child_value(1).get_child_value(2)
            value = ref_metadata.lookup_value("xa.data", GLib.VariantType.new("(tts)"))
        if value is None:
            continue
        installed = _from_endian(value.get_child_value(0).get_uint64(), 8, "big")
        download = _from_endian(value.get_child_value(1).get_uint64(), 8, "big")
        sizes[arch] = BuildSize(arch, download, installed)
    return [sizes[arch] for arch in sorted(sizes) if arch not in ambiguous]


async def fetch_build_sizes(build_id: int, app_id: str) -> list[BuildSize]:
    if type(build_id) is not int or build_id < 0:
        return []
    try:
        # One request, bounded both in wall time and bytes. No repository pull.
        async with asyncio.timeout(_TIMEOUT):
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, follow_redirects=False
            ) as client:
                async with client.stream(
                    "GET",
                    f"https://dl.flathub.org/build-repo/{build_id}/summary",
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    if (
                        response.status_code != 200
                        or response.headers.get("Content-Encoding", "identity")
                        != "identity"
                    ):
                        return []
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(data) + len(chunk) > _MAX_SUMMARY:
                            return []
                        data.extend(chunk)
                return parse_build_sizes(bytes(data), app_id)
    except Exception:
        # Optional enrichment must never prevent the existing success comment.
        logger.warning(
            "Build size metadata unavailable", build_id=build_id, exc_info=True
        )
        return []


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        if amount < 1024 or unit == "EiB":
            return f"{int(amount)} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def format_build_sizes(sizes: list[BuildSize]) -> str:
    if not sizes:
        return ""
    rows = "\n".join(
        f"| {size.arch} | {_format_bytes(size.download)} | {_format_bytes(size.installed)} |"
        for size in sizes
    )
    return (
        "\n\n<details><summary>Build details</summary>\n\n"
        "Flatpak sizes for the app ref only; excludes runtimes and extensions. "
        "Includes declared extra-data sizes. Download is the full download, not an update delta; "
        "installed size is an estimate. Only architectures with available size metadata are shown.\n\n"
        "| Architecture | Download | Installed |\n"
        "| --- | ---: | ---: |\n"
        f"{rows}\n\n</details>"
    )
