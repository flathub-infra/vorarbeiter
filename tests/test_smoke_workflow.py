import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def outcome(jobs, arches=("x86_64",), receipts=None, api_unavailable=False):
    workflow = yaml.safe_load(Path(".github/workflows/build.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["callback"]["steps"]
        if step.get("id") == "core"
    )
    script = (
        """
const github = {rest: {actions: {}}, paginate: async () => {
  if (process.env.API_UNAVAILABLE === 'true') throw new Error('API unavailable');
  return JSON.parse(process.env.JOBS);
}};
const context = {repo: {}, runId: 1};
const core = {setOutput: (name, value) => console.log(value)};
(async () => {
"""
        + step["with"]["script"]
        + "\n})();"
    )
    result = subprocess.run(
        ["node", "-e", script],
        text=True,
        capture_output=True,
        check=True,
        env={
            "PATH": os.environ["PATH"],
            "JOBS": json.dumps(jobs),
            "GITHUB_RUN_ATTEMPT": "2",
            "BUILD_MATRIX": json.dumps(
                {"include": [{"arch": arch} for arch in arches]}
            ),
            "CORE_UPLOADS": json.dumps(receipts or {}),
            "API_UNAVAILABLE": str(api_unavailable).lower(),
        },
    )
    return result.stdout.strip()


def job(arch, job_id, upload):
    return {
        "id": job_id,
        "name": f"build-{arch}",
        "conclusion": "failure",
        "steps": [
            {"name": "Build", "conclusion": "success"},
            {"name": "Validate build", "conclusion": "success"},
            {"name": "Upload build", "conclusion": upload},
            {"name": "Upload application-check inputs", "conclusion": "failure"},
        ],
    }


@pytest.mark.parametrize(
    "upload,conclusion,expected",
    [
        ("success", "failure", "success"),
        ("success", "cancelled", "success"),
        ("failure", "failure", "failure"),
        ("cancelled", "cancelled", "cancelled"),
    ],
)
def test_publication_depends_on_upload_not_later_check_preparation(
    upload, conclusion, expected
):
    build = job("x86_64", 1, upload)
    build["conclusion"] = conclusion
    assert outcome([build]) == expected


def test_upload_receipts_survive_check_preparation_timeout_and_api_failure():
    assert (
        outcome([], receipts={"x86_64": "success"}, api_unavailable=True) == "success"
    )


def test_partial_rerun_uses_latest_execution_for_each_architecture():
    jobs = [
        job("x86_64", 1, "success"),
        job("aarch64", 2, "failure"),
        job("aarch64", 3, "success"),
    ]
    assert outcome(jobs, arches=("x86_64", "aarch64")) == "success"


@pytest.mark.parametrize(
    "config,enabled",
    [
        ({}, "false"),
        ({"smoke-test": {"enabled": False}}, "false"),
        ({"smoke-test": {"enabled": True}}, "true"),
        ({"smoke-test": {"enabled": "invalid"}}, "true"),
    ],
)
def test_matrix_preserves_architectures_and_emits_separate_opt_in(
    config, enabled, tmp_path
):
    workflow = yaml.safe_load(Path(".github/workflows/build.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["setup-matrix"]["steps"]
        if step.get("id") == "set-matrix"
    )
    script = re.sub(r"\$\{\{.*?\}\}", "default", step["run"])
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo/flathub.json").write_text(
        json.dumps({"only-arches": ["x86_64"], **config})
    )
    outputs = tmp_path / "outputs"
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        check=True,
        env={"GITHUB_OUTPUT": str(outputs)},
    )
    values = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    assert values["smoke-enabled"] == enabled
    assert json.loads(values["matrix"]) == {
        "include": [
            {"arch": "x86_64", "runner": "ubuntu-24.04", "timeout-minutes": 360}
        ]
    }
