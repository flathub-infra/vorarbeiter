"""Dependency-free input contract shared by CI and application-check reporting."""

import argparse
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

MAX_INPUT = 2 * 1024**3
MAX_RESULT = 64 * 1024**2
MAX_JSON = 256 * 1024
MAX_PNG = 8 * 1024**2
MAX_ENTRIES = 500
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def relative_path(value: str) -> str:
    """Accept only portable paths beneath an artifact or checkout root."""
    if (
        not isinstance(value, str)
        or not value
        or any(c in value for c in "\x00\r\n\\")
        or PurePosixPath(value).is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("expected a relative path without traversal")
    return value


def read_json(path: Path):
    if path.is_symlink() or path.stat().st_size > MAX_JSON:
        raise ValueError("invalid or oversized JSON file")
    return json.loads(path.read_text())


def smoke_config(document: dict) -> dict | None:
    """Validate the app-owned opt-in, without accepting execution options."""
    if not isinstance(document, dict):
        raise TypeError("flathub.json must be an object")
    config = document.get("smoke-test")
    if config is None:
        return None
    if not isinstance(config, dict) or set(config) - {"enabled", "screenshot-recipe"}:
        raise ValueError("smoke-test must contain only enabled and screenshot-recipe")
    if not isinstance(config.get("enabled"), bool):
        raise TypeError("smoke-test.enabled must be a boolean")
    recipe = config.get("screenshot-recipe")
    if recipe is not None:
        relative_path(recipe)
    return config if config["enabled"] else None


def prepare_inputs(work: Path, output: Path, app_id: str, arch: str) -> None:
    """Transport the existing app build and its tracked sources, never a rebuild."""
    config_path = work / "flathub.json"
    if not config_path.exists():
        return
    request = {"schema_version": 1, "arch": arch}
    try:
        config = smoke_config(read_json(config_path))
        if config is None:
            return
        if arch != "x86_64":
            request.update(
                state="skipped", message="Only x86_64 smoke images are available"
            )
            return
        recipe = config.get("screenshot-recipe")
        if recipe:
            for path in (work / recipe, *(work / recipe).parents):
                if path == work.parent:
                    break
                if path.is_symlink():
                    raise ValueError("screenshot recipe must not contain symlinks")
            if not (work / recipe).is_file():
                raise ValueError("screenshot recipe does not exist")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]+", app_id):
            raise ValueError("invalid app ID")
        request.update(state="infrastructure_error", message="Input preparation failed")
        output.mkdir(parents=True, exist_ok=True)
        sha = subprocess.check_output(
            ["git", "-C", str(work), "rev-parse", "HEAD"], text=True, timeout=30
        ).strip()
        refs = list((work / "repo/refs/heads/app" / app_id / arch).glob("*"))
        if len(refs) != 1 or not refs[0].is_file() or refs[0].is_symlink():
            raise ValueError("expected one exported app ref for this architecture")
        app_ref = refs[0].relative_to(work / "repo/refs/heads").as_posix()
        # Git archive excludes untracked files, including builder state and credentials.
        with (output / "source.tar").open("wb") as source:
            subprocess.run(
                ["git", "-C", str(work), "archive", "HEAD"],
                stdout=source,
                check=True,
                timeout=60,
            )
        if (output / "source.tar").stat().st_size > MAX_INPUT:
            raise ValueError("smoke input exceeds 2 GiB")
        total = 0
        with tarfile.open(output / "input.tar", "w", dereference=True) as archive:
            with tarfile.open(output / "source.tar") as source:
                for member in source:
                    if not member.isfile() and not member.isdir():
                        raise ValueError(
                            "source snapshot contains a link or special file"
                        )
                    relative_path(member.name.rstrip("/"))
                    total += member.size + 1024
                    if total > MAX_INPUT:
                        raise ValueError("smoke input exceeds 2 GiB")
                    member.name = "source/" + member.name
                    archive.addfile(
                        member, source.extractfile(member) if member.isfile() else None
                    )
            for path in sorted((work / "repo").rglob("*")):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise ValueError(
                        "exported repository contains a link or special file"
                    )
                total += (path.stat().st_size if path.is_file() else 0) + 1024
                if total > MAX_INPUT:
                    raise ValueError("smoke input exceeds 2 GiB")
                archive.add(
                    path, arcname=path.relative_to(work).as_posix(), recursive=False
                )
        if (output / "input.tar").stat().st_size > MAX_INPUT:
            raise ValueError("smoke input exceeds 2 GiB")
        request.update(
            state="ready",
            message="",
            sha=sha,
            app_ref=app_ref,
            recipe="source/" + recipe if recipe else "",
        )
    except (
        ValueError,
        TypeError,
        OSError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ) as error:
        state = request.get("state", "configuration_error")
        request.update(state=state, message=str(error)[:1000])
    finally:
        if "state" in request:
            output.mkdir(parents=True, exist_ok=True)
            (output / "source.tar").unlink(missing_ok=True)
            if request["state"] != "ready":
                (output / "input.tar").unlink(missing_ok=True)
            (output / "request.json").write_text(json.dumps(request))


def unpack_inputs(source: Path, destination: Path) -> dict:
    """Extract bounded regular members, rejecting links and overlapping names."""
    request = read_json(source / "request.json")
    if request.get("state") != "ready":
        return request
    if not re.fullmatch(
        r"app/[A-Za-z0-9_.-]+/x86_64/[A-Za-z0-9_.-]+", request["app_ref"]
    ):
        raise ValueError("invalid exported app ref")
    if not re.fullmatch(r"[a-f0-9]{40}", request["sha"]):
        raise ValueError("invalid source revision")
    if request["recipe"]:
        relative_path(request["recipe"])
    archive_path = source / "input.tar"
    if archive_path.stat().st_size > MAX_INPUT:
        raise ValueError("smoke input exceeds 2 GiB")
    total = 0
    names = set()
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive:
            name = relative_path(member.name.rstrip("/"))
            if name in names or not (member.isfile() or member.isdir()):
                raise ValueError("duplicate, link, or special file in smoke input")
            if name.split("/")[0] not in {"source", "repo"}:
                raise ValueError("unexpected smoke input path")
            names.add(name)
            if len(names) > 200_000:
                raise ValueError("too many smoke input members")
            total += member.size
            if total > MAX_INPUT:
                raise ValueError("smoke input exceeds 2 GiB")
            path = destination / name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                data = archive.extractfile(member)
                assert data is not None
                with data, path.open("xb") as target:
                    shutil.copyfileobj(data, target)
    return request


def collect_results(source: Path, results: Path, output: Path, outcomes: dict) -> None:
    """Keep diagnostics bounded and copy only regular files into the result artifact."""
    output.mkdir(parents=True, exist_ok=True)
    try:
        request = read_json(source / "request.json")
    except (ValueError, OSError):
        request = {"state": "infrastructure_error", "message": "Missing smoke inputs"}
    report = {"schema_version": 1, "request": request, "outcomes": outcomes}
    total = 0
    count = 0
    for mode in ("verify", "screenshots"):
        root = results / mode
        if root.is_symlink():
            continue
        for path in sorted(root.rglob("*")) if root.exists() else []:
            relative = path.relative_to(results)
            if (
                path.is_symlink()
                or not path.is_file()
                or any(p.is_symlink() for p in path.parents)
            ):
                continue
            size = path.stat().st_size
            if (
                size > MAX_PNG
                or total + size > MAX_RESULT // 2
                or count >= MAX_ENTRIES - 1
            ):
                continue
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            total += size
            count += 1
    (output / "report.json").write_text(json.dumps(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("work", type=Path)
    prepare.add_argument("output", type=Path)
    prepare.add_argument("app_id")
    prepare.add_argument("arch")
    unpack = sub.add_parser("unpack")
    unpack.add_argument("source", type=Path)
    unpack.add_argument("destination", type=Path)
    collect = sub.add_parser("collect")
    collect.add_argument("source", type=Path)
    collect.add_argument("results", type=Path)
    collect.add_argument("output", type=Path)
    collect.add_argument("verify_outcome")
    collect.add_argument("screenshot_outcome")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare_inputs(args.work.resolve(), args.output, args.app_id, args.arch)
    elif args.command == "unpack":
        request = unpack_inputs(args.source, args.destination)
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"ready={str(request.get('state') == 'ready').lower()}\n")
            if request.get("state") == "ready":
                stream.write(
                    f"app-ref={request['app_ref']}\nrecipe={request['recipe']}\n"
                )
    else:
        collect_results(
            args.source,
            args.results,
            args.output,
            {"verify": args.verify_outcome, "screenshots": args.screenshot_outcome},
        )


if __name__ == "__main__":
    main()
