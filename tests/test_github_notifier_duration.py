from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import Pipeline
from app.services.github_notifier import GitHubNotifier
from app.utils.flat_manager import FlatManagerClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["committed", "failure", "cancelled", "commit_failure"]
)
@pytest.mark.parametrize("with_flat_manager", [False, True])
async def test_completion_preserves_comment_and_adds_recorded_elapsed(
    status, with_flat_manager
):
    pipeline = Pipeline(
        app_id="org.test.App",
        params={"repo": "flathub/flathub", "pr_number": "42"},
        build_id=123,
        commit_job_id=456,
        flat_manager_repo="test",
        log_url="https://example.com/runs/123",
    )
    flat_manager = MagicMock(spec=FlatManagerClient) if with_flat_manager else None
    if flat_manager:
        flat_manager.get_flatpakref_url.return_value = (
            "https://example.com/app.flatpakref"
        )
    notifier = GitHubNotifier(flat_manager)
    with (
        patch("app.services.github_notifier.create_pr_comment") as post,
        patch(
            "app.services.github_notifier.get_build_job_arches", return_value=["x86_64"]
        ),
        patch(
            "app.services.github_notifier.get_linter_warning_messages",
            return_value=["warning"],
        ),
        patch("app.services.github_notifier.get_penalty_until", return_value=None),
    ):
        await notifier.notify_pr_build_complete(pipeline, status)
        original = post.call_args.kwargs["comment"]
        # Historical timestamps ensure notification/commit delays are excluded.
        started_at = datetime(2020, 1, 1, tzinfo=UTC)
        pipeline.started_at = started_at
        pipeline.finished_at = started_at + timedelta(seconds=754)
        await notifier.notify_pr_build_complete(pipeline, status)

    assert post.await_count == 2
    assert (
        post.call_args.kwargs["comment"] == original + "\n\n*Build duration: 12m 34s.*"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("started_at", "finished_at", "expected"),
    [
        (None, None, None),
        (None, datetime(2020, 1, 1), None),
        (datetime(2020, 1, 1), None, None),
        ("invalid", datetime(2020, 1, 1), None),
        (datetime(2020, 1, 1), "invalid", None),
        (datetime(2020, 1, 2), datetime(2020, 1, 1), None),
        (datetime(2020, 1, 1, microsecond=1), datetime(2020, 1, 1), None),
        (datetime(2020, 1, 1), datetime(2020, 1, 1), "<1s"),
        (datetime(2020, 1, 1), datetime(2020, 1, 1, microsecond=999999), "<1s"),
        (datetime(2020, 1, 1), datetime(2020, 1, 1, second=59), "59s"),
        (datetime(2020, 1, 1), datetime(2020, 1, 1, minute=1), "1m 0s"),
        (datetime(2020, 1, 1), datetime(2020, 1, 1, hour=1), "1h 0m 0s"),
        (
            datetime(2020, 1, 1),
            datetime(2020, 1, 2, hour=1, minute=2, second=3),
            "25h 2m 3s",
        ),
        (datetime(2020, 1, 1), datetime(2020, 1, 1, minute=1, tzinfo=UTC), "1m 0s"),
        (datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 1, 1, minute=1), "1m 0s"),
        (
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 1, 1, hour=2, minute=1, tzinfo=timezone(timedelta(hours=2))),
            "1m 0s",
        ),
    ],
)
async def test_completion_duration_handles_timestamp_variants(
    started_at, finished_at, expected
):
    pipeline = Pipeline(
        params={"repo": "flathub/org.test.App", "pr_number": "42"},
        log_url=None,
        started_at=started_at,
        finished_at=finished_at,
    )
    with patch("app.services.github_notifier.create_pr_comment") as post:
        await GitHubNotifier().notify_pr_build_complete(pipeline, "committed")

    post.assert_awaited_once()
    comment = post.call_args.kwargs["comment"]
    assert comment == "✅ [Test build succeeded](None)." + (
        f"\n\n*Build duration: {expected}.*" if expected else ""
    )
