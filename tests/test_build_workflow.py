import os
import subprocess
from pathlib import Path

import pytest
import yaml


def _verification_script(job: str) -> str:
    workflow = yaml.safe_load(Path(".github/workflows/build.yml").read_text())
    steps = workflow["jobs"][job]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Verify submission checkout"
    )
    return script.replace("${{ inputs.callback_url }}", "https://callback.invalid")


def _commit(repo: Path, content: str) -> str:
    (repo / "manifest.yml").write_text(content)
    subprocess.run(["git", "add", "manifest.yml"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            content,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_verification(tmp_path: Path, job: str, expected_sha: str):
    event_path = tmp_path / "event.json"
    event_path.write_text('{"inputs":{"callback_token":"test-token"}}')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_log = tmp_path / "curl.log"
    (bin_dir / "jq").write_text("#!/bin/sh\necho test-token\n")
    (bin_dir / "curl").write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CURL_LOG"\n')
    (bin_dir / "jq").chmod(0o755)
    (bin_dir / "curl").chmod(0o755)
    env = {
        **os.environ,
        "CURL_LOG": str(curl_log),
        "EXPECTED_SUBMISSION_SHA": expected_sha,
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_WORKSPACE": str(tmp_path),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _verification_script(job)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, curl_log


@pytest.mark.parametrize("job", ["validate-manifest", "build"])
def test_workflow_accepts_matching_checkout(tmp_path, job):
    repo = tmp_path / "work"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    expected_sha = _commit(repo, "first")

    result, curl_log = _run_verification(tmp_path, job, expected_sha)

    assert result.returncode == 0, result.stderr
    if job == "validate-manifest":
        assert expected_sha in curl_log.read_text()
    else:
        assert not curl_log.exists()


@pytest.mark.parametrize("job", ["validate-manifest", "build"])
def test_workflow_rejects_pr_ref_that_moves_before_checkout(tmp_path, job):
    repo = tmp_path / "work"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    expected_sha = _commit(repo, "first")
    actual_sha = _commit(repo, "second")

    result, curl_log = _run_verification(tmp_path, job, expected_sha)

    assert result.returncode != 0
    assert (
        f"Checkout SHA {actual_sha} does not match expected submission SHA {expected_sha}"
        in result.stdout
    )
    assert not curl_log.exists()
