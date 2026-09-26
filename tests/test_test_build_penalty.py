from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.pipeline import Pipeline, PipelineStatus
from app.models.webhook_event import WebhookEvent, WebhookSource
from app.services.test_build_penalty import get_penalty_until, is_penalty_exempt

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


async def add_builds(
    db_session_maker,
    statuses,
    *,
    repo="flathub/org.example.App",
    pr=4,
    actors=None,
    age=0,
):
    async with db_session_maker() as db:
        for index, status in enumerate(statuses):
            event = WebhookEvent(
                source=WebhookSource.GITHUB,
                repository=repo,
                actor=(actors or {}).get(index, "contributor"),
                payload={},
            )
            db.add(event)
            await db.flush()
            finished = NOW - timedelta(minutes=age + len(statuses) - 1 - index)
            db.add(
                Pipeline(
                    app_id=repo.split("/")[-1],
                    params={"repo": repo, "pr_number": str(pr)},
                    flat_manager_repo="test",
                    status=status,
                    webhook_event_id=event.id,
                    created_at=finished,
                    finished_at=finished,
                )
            )
        await db.commit()


@pytest.mark.asyncio
async def test_three_failures_pause_until_latest_finish(db_session_maker):
    await add_builds(db_session_maker, [PipelineStatus.FAILED] * 3)
    assert await get_penalty_until(
        "flathub/org.example.App", 4, NOW
    ) == NOW + timedelta(minutes=60)
    assert (
        await get_penalty_until(
            "flathub/org.example.App", 4, NOW + timedelta(minutes=60)
        )
        is None
    )


@pytest.mark.asyncio
async def test_short_streak_and_success_reset(db_session_maker):
    await add_builds(db_session_maker, [PipelineStatus.FAILED] * 2)
    assert await get_penalty_until("flathub/org.example.App", 4, NOW) is None
    await add_builds(
        db_session_maker, [PipelineStatus.SUCCEEDED, PipelineStatus.FAILED], age=-2
    )
    assert await get_penalty_until("flathub/org.example.App", 4, NOW) is None


@pytest.mark.asyncio
async def test_cancelled_superseded_and_running_do_not_reset(db_session_maker):
    await add_builds(
        db_session_maker,
        [
            PipelineStatus.FAILED,
            PipelineStatus.CANCELLED,
            PipelineStatus.FAILED,
            PipelineStatus.SUPERSEDED,
            PipelineStatus.FAILED,
            PipelineStatus.RUNNING,
        ],
    )
    assert await get_penalty_until(
        "flathub/org.example.App", 4, NOW
    ) == NOW + timedelta(minutes=59)


@pytest.mark.asyncio
async def test_expired_streak(db_session_maker):
    await add_builds(db_session_maker, [PipelineStatus.FAILED] * 3, age=61)
    assert await get_penalty_until("flathub/org.example.App", 4, NOW) is None


@pytest.mark.asyncio
async def test_bot_failures_do_not_count(db_session_maker):
    await add_builds(
        db_session_maker, [PipelineStatus.FAILED] * 3, actors={1: "flathubbot"}
    )
    assert await get_penalty_until("flathub/org.example.App", 4, NOW) is None


@pytest.mark.asyncio
async def test_other_pr_and_repo_do_not_count(db_session_maker):
    await add_builds(db_session_maker, [PipelineStatus.FAILED], pr=4)
    await add_builds(db_session_maker, [PipelineStatus.FAILED], pr=5)
    await add_builds(
        db_session_maker, [PipelineStatus.FAILED], repo="other/org.example.App"
    )
    assert await get_penalty_until("flathub/org.example.App", 4, NOW) is None


@pytest.mark.asyncio
async def test_disabled_limit_and_exemptions(db_session_maker):
    await add_builds(db_session_maker, [PipelineStatus.FAILED] * 3)
    with patch.object(settings, "test_build_failure_streak_limit", 0):
        assert await get_penalty_until("flathub/org.example.App", 4, NOW) is None
        assert is_penalty_exempt("org.example.App", None)
    assert is_penalty_exempt("org.chromium.Chromium", None)
    assert is_penalty_exempt("org.example.App", "flathubbot")
    assert is_penalty_exempt("org.example.App", "github-actions[bot]")
    assert not is_penalty_exempt("org.example.App", "contributor")
