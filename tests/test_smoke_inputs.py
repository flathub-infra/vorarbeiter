import io
import json
import subprocess
import tarfile

import pytest

from app.smoke import prepare_inputs, unpack_inputs


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


@pytest.mark.parametrize(
    "config",
    [
        True,
        {"enabled": "yes"},
        {"enabled": True, "image": "custom"},
        {"enabled": True, "screenshot-recipe": "../outside"},
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
