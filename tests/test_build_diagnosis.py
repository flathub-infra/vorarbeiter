import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select

from app.config import settings
from app.models import BuildDiagnosis, Pipeline, PipelineStatus
from app.pipelines.build import BuildPipeline
from app.queue import app as queue_app


@pytest.fixture
def connector(db_session_maker):
    @asynccontextmanager
    async def get_test_db(*, use_replica: bool = False):
        async with db_session_maker() as db:
            yield db
            await db.commit()

    connector = InMemoryConnector()
    with (
        patch("app.pipelines.build.get_db", get_test_db),
        patch.object(settings, "ff_build_diagnosis", True),
        patch(
            "app.services.github_actions.GitHubActionsService.check_run_was_cancelled",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.pipelines.build.GitHubNotifier.handle_build_completion",
            AsyncMock(),
        ),
        queue_app.replace_connector(connector),
    ):
        yield connector


async def failure_callback(db_session_maker) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_session_maker() as db:
        db.add(
            Pipeline(
                id=pipeline_id,
                app_id="org.test.App",
                status=PipelineStatus.RUNNING,
                params={"repo": "flathub/org.test.App"},
                provider_data={},
                callback_token="token",
            )
        )
        await db.commit()
    build = BuildPipeline()
    build.start_pending_builds = AsyncMock(return_value=[])
    await build.handle_status_callback(pipeline_id, {"status": "failure"})
    return pipeline_id


@pytest.mark.asyncio
async def test_failure_defers_one_job(db_session_maker, connector):
    pipeline_id = await failure_callback(db_session_maker)

    jobs = list(connector.jobs.values())
    assert [(j["task_name"], j["queue_name"]) for j in jobs] == [
        ("app.tasks.diagnose_build", "build-diagnosis")
    ]
    assert jobs[0]["args"] == {"pipeline_id": str(pipeline_id)}
    async with db_session_maker() as db:
        assert await db.get(BuildDiagnosis, pipeline_id) is not None


@pytest.mark.asyncio
async def test_repeated_callback_defers_none(db_session_maker, connector):
    pipeline_id = await failure_callback(db_session_maker)
    build = BuildPipeline()
    build.start_pending_builds = AsyncMock(return_value=[])

    await build.handle_status_callback(pipeline_id, {"status": "failure"})

    assert len(connector.jobs) == 1
    async with db_session_maker() as db:
        rows = (await db.execute(select(BuildDiagnosis))).scalars().all()
        pipeline = await db.get(Pipeline, pipeline_id)
    assert len(rows) == 1
    assert pipeline is not None
    assert pipeline.status == PipelineStatus.FAILED
