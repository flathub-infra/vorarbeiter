from unittest.mock import patch

import pytest

from app.models import Pipeline
from app.services.github_notifier import GitHubNotifier


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["started", "committed", "failure", "cancelled", "commit_failure", "superseded"],
)
@pytest.mark.parametrize("repo", ["flathub/org.example.App", "flathub/flathub"])
async def test_build_comments_link_saved_revision(status, repo):
    sha = "abcdef01" * 5
    pipeline = Pipeline(
        app_id="org.example.App",
        params={
            "repo": repo,
            "pr_number": "42",
            "ref": "refs/pull/42/head",
            "sha": sha,
            "base_sha": "b" * 40,
        },
        log_url="https://github.com/flathub-infra/vorarbeiter/actions/runs/123",
    )
    notifier = GitHubNotifier()
    with (
        patch("app.services.github_notifier.create_pr_comment") as comment,
        patch("app.services.github_notifier.update_commit_status"),
        patch(
            "app.services.github_notifier.get_linter_warning_messages", return_value=[]
        ),
        patch("app.services.github_notifier.get_build_job_arches", return_value=[]),
    ):
        if status == "started":
            assert pipeline.log_url is not None
            await notifier.notify_pr_build_started(pipeline, pipeline.log_url)
        elif status == "superseded":
            await notifier.handle_build_superseded(pipeline)
        else:
            await notifier.notify_pr_build_complete(pipeline, status)

    comment.assert_awaited_once()
    body = comment.call_args.kwargs["comment"]
    assert body.endswith(
        f"Requested commit: [`abcdef0`](https://github.com/{repo}/commit/{sha})."
    )
    assert body.count("/commit/") == 1
    assert comment.call_args.kwargs["git_repo"] == repo
    assert "b" * 40 not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata, expected_sha",
    [
        ({"sha": "a" * 40}, "a" * 40),
        ({"sha": "a" * 40, "verified_sha": "b" * 40}, "b" * 40),
        ({"verified_sha": "C" * 40}, "c" * 40),
        ({"sha": "a" * 40, "verified_sha": "invalid"}, "a" * 40),
        ({}, None),
        ({"sha": None}, None),
        ({"sha": 123}, None),
        ({"sha": "abc123"}, None),
        ({"sha": "0" * 40}, None),
        ({"sha": "a" * 40, "repo": "invalid"}, None),
        ({"sha": "a" * 40, "repo": "flathub/.."}, None),
        ({"sha": "a" * 40, "repo": "flathub/app)\nmalformed"}, None),
    ],
)
async def test_commit_metadata_is_optional(metadata, expected_sha):
    pipeline = Pipeline(
        params={"repo": "flathub/org.example.App", "pr_number": "42", **metadata},
    )
    with patch("app.services.github_notifier.create_pr_comment") as comment:
        await GitHubNotifier().notify_pr_build_started(
            pipeline, "https://example.com/log"
        )

    comment.assert_awaited_once()
    body = comment.call_args.kwargs["comment"]
    original = "🚧 Started [test build](https://example.com/log)."
    if expected_sha:
        label = (
            "Built commit"
            if metadata.get("verified_sha") in {"b" * 40, "C" * 40}
            else "Requested commit"
        )
        assert body == (
            original
            + f"\n\n{label}: [`{expected_sha[:7]}`]"
            + f"(https://github.com/flathub/org.example.App/commit/{expected_sha})."
        )
    else:
        assert body == original
