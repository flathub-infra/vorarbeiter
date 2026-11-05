from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.models import Pipeline
from app.services.github_notifier import GitHubNotifier, _architecture_overview
from app.utils.github import BuildJob, get_build_jobs


@pytest.mark.asyncio
async def test_jobs_keep_outcomes_links_and_paginate_latest_attempt():
    client = Mock(
        request=AsyncMock(
            side_effect=[
                Mock(json=lambda: {"jobs": [{"name": "callback"}] * 100}),
                Mock(
                    json=lambda: {
                        "jobs": [
                            {
                                "name": "build-x86_64",
                                "status": "completed",
                                "conclusion": "failure",
                                "html_url": "https://github.com/flathub-infra/vorarbeiter/actions/runs/123/job/7",
                            },
                            {
                                "name": "build-aarch64",
                                "status": "queued",
                                "conclusion": None,
                            },
                            {"name": "build"},
                            {"name": "build-${{ matrix.arch }}"},
                            {"name": "validate-manifest", "conclusion": "failure"},
                        ]
                    }
                ),
            ]
        )
    )
    with patch("app.utils.github.get_github_client", return_value=client):
        jobs = await get_build_jobs(123)
    assert jobs == [
        BuildJob(
            "x86_64",
            "completed",
            "failure",
            "https://github.com/flathub-infra/vorarbeiter/actions/runs/123/job/7",
        ),
        BuildJob("aarch64", "queued", None, None),
    ]
    assert [call.kwargs["params"] for call in client.request.call_args_list] == [
        {"filter": "latest", "per_page": 100, "page": 1},
        {"filter": "latest", "per_page": 100, "page": 2},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response", [None, Mock(json=dict), Mock(json=lambda: {"jobs": None})]
)
async def test_unavailable_jobs_are_not_an_empty_matrix(response):
    with patch("app.utils.github.get_github_client") as factory:
        factory.return_value.request = AsyncMock(return_value=response)
        assert await get_build_jobs(123) is None


@pytest.mark.asyncio
async def test_partial_fetch_failure_discards_incomplete_overview():
    with patch("app.utils.github.get_github_client") as factory:
        factory.return_value.request = AsyncMock(
            side_effect=[
                Mock(json=lambda: {"jobs": [{"name": "build-x86_64"}] * 100}),
                RuntimeError("unavailable"),
            ]
        )
        assert await get_build_jobs(123) is None


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("completed", "success", "succeeded"),
        ("completed", "failure", "failed"),
        ("completed", "cancelled", "cancelled"),
        ("completed", "skipped", "skipped"),
        ("completed", "timed_out", "timed out"),
        ("completed", "neutral", "neutral"),
        ("completed", None, "unknown"),
        ("completed", "new-conclusion", "unknown"),
        ("queued", None, "pending"),
        ("waiting", None, "pending"),
        ("in_progress", None, "in progress"),
        (None, None, "unknown"),
        ("queued", "success", "pending"),
    ],
)
def test_outcomes_never_infer_success(status, conclusion, expected):
    assert _architecture_overview([BuildJob("aarch64", status, conclusion, None)]) == (
        f"\n\n**Architectures:** aarch64: {expected}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["committed", "failure", "cancelled", "commit_failure"]
)
@pytest.mark.parametrize("available", [True, False])
async def test_final_comments_preserve_body_with_optional_outcomes(status, available):
    pipeline = Pipeline(
        app_id="org.test.App",
        params={"repo": "flathub/org.test.App", "pr_number": "42"},
        log_url="https://github.com/flathub-infra/vorarbeiter/actions/runs/123",
    )
    response = (
        Mock(
            json=lambda: {
                "jobs": [
                    {
                        "name": "build-x86_64",
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": "https://github.com/job/7",
                    },
                    {
                        "name": "build-aarch64",
                        "status": "completed",
                        "conclusion": "cancelled",
                        "html_url": "https://github.com/job/8",
                    },
                ]
            }
        )
        if available
        else None
    )
    with (
        patch("app.utils.github.get_github_client") as factory,
        patch(
            "app.services.github_notifier.get_linter_warning_messages", return_value=[]
        ),
        patch("app.services.github_notifier.create_pr_comment") as post,
    ):
        factory.return_value.request = AsyncMock(return_value=response)
        await GitHubNotifier().notify_pr_build_complete(pipeline, status)
    body = post.call_args.kwargs["comment"]
    assert ("**Architectures:**" in body) == available
    if available:
        assert (
            "[aarch64](https://github.com/job/8): cancelled · [x86_64](https://github.com/job/7): failed"
            in body
        )
    if status != "committed":
        assert "<summary>Help</summary>" in body
    if status == "commit_failure":
        assert "infrastructure issue" in body


@pytest.mark.parametrize("jobs", [None, []])
def test_missing_outcomes_omit_overview(jobs):
    assert _architecture_overview(jobs) == ""
