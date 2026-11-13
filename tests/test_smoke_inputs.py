import io
import json
import subprocess
import tarfile

import pytest

from app.smoke import collect_results, prepare_inputs, unpack_inputs


def commit_inputs(work):
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(work),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-m",
            "Fixture",
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    "recipe,error",
    [
        ("missing.yml", "screenshot recipe does not exist"),
        ("../outside", "expected a relative path without traversal"),
        ("", "expected a relative path without traversal"),
        (123, "expected a relative path without traversal"),
        ("untracked.yml", "screenshot recipe is not tracked"),
        ("linked.yml", "screenshot recipe must not contain symlinks"),
        ("linked-dir/recipe.yml", "screenshot recipe must not contain symlinks"),
    ],
)
def test_invalid_recipe_preserves_startup_input(tmp_path, recipe, error):
    work = tmp_path / "work"
    work.mkdir()
    (work / "flathub.json").write_text(
        json.dumps({"smoke-test": {"enabled": True, "screenshot-recipe": recipe}})
    )
    if recipe == "linked.yml":
        (work / "real.yml").write_text("version: 1\n")
        (work / recipe).symlink_to("real.yml")
    if recipe == "linked-dir/recipe.yml":
        (work / "real-dir").mkdir()
        (work / "real-dir/recipe.yml").write_text("version: 1\n")
        (work / "linked-dir").symlink_to("real-dir", target_is_directory=True)
    commit_inputs(work)
    (work / "untracked.yml").write_text("version: 1\n")
    ref = work / "repo/refs/heads/app/org.example.App/x86_64/test"
    ref.parent.mkdir(parents=True)
    ref.write_text("commit")
    output = tmp_path / "output"
    prepare_inputs(work, output, "org.example.App", "x86_64")
    request = unpack_inputs(output, tmp_path / "unpacked")
    assert request["state"] == "ready"
    assert request["recipe"] == ""
    assert request["recipe_error"] == error
    assert not any(path.is_symlink() for path in (tmp_path / "unpacked").rglob("*"))


def test_prepare_uses_built_repo_and_tracked_recipe_snapshot(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "flathub.json").write_text(
        json.dumps(
            {"smoke-test": {"enabled": True, "screenshot-recipe": "ci/screenshots.yml"}}
        )
    )
    (work / "ci").mkdir()
    (work / "ci/screenshots.yml").write_text("version: 1\n")
    commit_inputs(work)
    ref = work / "repo/refs/heads/app/org.example.App/x86_64/test"
    ref.parent.mkdir(parents=True)
    ref.write_text("built commit")
    (work / "repo/refs/remotes").mkdir()
    (work / "untracked-token").write_text("must not be transported")
    output = tmp_path / "output"
    prepare_inputs(work, output, "org.example.App", "x86_64")
    request = json.loads((output / "request.json").read_text())
    assert request["state"] == "ready"
    assert request["app_ref"] == "app/org.example.App/x86_64/test"
    assert request["recipe"] == "source/ci/screenshots.yml"
    with tarfile.open(output / "input.tar") as archive:
        assert "source/ci/screenshots.yml" in archive.getnames()
        assert "repo/refs/heads/app/org.example.App/x86_64/test" in archive.getnames()
        assert "repo/refs/remotes" in archive.getnames()
        assert all("untracked-token" not in name for name in archive.getnames())
    unpack_inputs(output, tmp_path / "unpacked")
    assert (tmp_path / "unpacked/repo/refs/remotes").is_dir()


def test_disabled_creates_no_input_artifact(tmp_path):
    prepare_inputs(tmp_path, tmp_path / "out", "org.example.App", "x86_64")
    assert not (tmp_path / "out/request.json").exists()


def test_collector_prioritizes_results_and_enforces_json_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("app.smoke.MAX_ENTRIES", 4)
    monkeypatch.setattr("app.smoke.MAX_JSON", 256)
    results = tmp_path / "results"
    (results / "verify/logs").mkdir(parents=True)
    (results / "verify/result.json").write_text('{"status":"passed"}')
    (results / "verify.execution.json").write_text('{"status":"completed"}')
    (results / "verify/logs/oversized.json").write_text(" " * 257)
    (results / "verify/logs/linked.json").symlink_to(results / "verify/result.json")
    (results / "verify/logs/log.txt").write_text("diagnostic")
    output = tmp_path / "out"
    collect_results(tmp_path, results, output, {})
    assert (output / "verify/result.json").exists()
    assert (output / "verify.execution.json").exists()
    assert (output / "verify/logs/log.txt").exists()
    assert not (output / "verify/logs/oversized.json").exists()
    assert not (output / "verify/logs/linked.json").exists()


@pytest.mark.parametrize(
    "config",
    [
        True,
        {"enabled": "yes"},
        {"enabled": True, "image": "custom"},
    ],
)
def test_bad_opt_in_produces_configuration_error(config, tmp_path):
    (tmp_path / "flathub.json").write_text(json.dumps({"smoke-test": config}))
    prepare_inputs(tmp_path, tmp_path / "out", "org.example.App", "x86_64")
    assert (
        json.loads((tmp_path / "out/request.json").read_text())["state"]
        == "configuration_error"
    )
    assert not (tmp_path / "out/input.tar").exists()


def test_unsupported_arch_is_explicitly_skipped(tmp_path):
    (tmp_path / "flathub.json").write_text('{"smoke-test": {"enabled": true}}')
    prepare_inputs(tmp_path, tmp_path / "out", "org.example.App", "aarch64")
    assert json.loads((tmp_path / "out/request.json").read_text())["state"] == "skipped"


@pytest.mark.parametrize(
    "refs,expected",
    [
        (["runtime/org.example.App/x86_64/stable"], "skipped"),
        (["runtime/org.example.App/x86_64/beta"], "skipped"),
        ([], "infrastructure_error"),
        (["runtime/org.other.App/x86_64/stable"], "infrastructure_error"),
        (
            [
                "runtime/org.example.App/x86_64/stable",
                "runtime/org.example.App/x86_64/beta",
            ],
            "infrastructure_error",
        ),
    ],
)
def test_non_launchable_export_is_skipped_but_missing_or_ambiguous_refs_are_not(
    tmp_path, refs, expected
):
    (tmp_path / "flathub.json").write_text('{"smoke-test": {"enabled": true}}')
    commit_inputs(tmp_path)
    for ref in refs:
        path = tmp_path / "repo/refs/heads" / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a" * 64)
    output = tmp_path / "out"
    prepare_inputs(tmp_path, output, "org.example.App", "x86_64")
    request = json.loads((output / "request.json").read_text())
    assert request["state"] == expected
    if expected == "skipped":
        assert "not launchable" in request["message"]
    assert not (output / "input.tar").exists()


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("source/link", tarfile.SYMTYPE),
        ("source/hardlink", tarfile.LNKTYPE),
        ("unexpected", tarfile.REGTYPE),
    ],
)
def test_unpack_rejects_unsafe_archive_members(tmp_path, name, kind):
    (tmp_path / "request.json").write_text(
        json.dumps(
            {
                "state": "ready",
                "app_ref": "app/org.example.App/x86_64/test",
                "sha": "a" * 40,
                "recipe": "",
            }
        )
    )
    with tarfile.open(tmp_path / "input.tar", "w") as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "/etc/passwd" if kind != tarfile.REGTYPE else ""
        archive.addfile(member, io.BytesIO())
    with pytest.raises(ValueError):
        unpack_inputs(tmp_path, tmp_path / "unpacked")
    assert not (tmp_path / "escape").exists()
