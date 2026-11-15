import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from procrastinate.testing import InMemoryConnector
from pydantic_ai.exceptions import ModelRateLimitError
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from app.config import settings
from app.models import BuildDiagnosis, BuildDiagnosisStatus, Pipeline, PipelineStatus
from app.pipelines.build import BuildPipeline
from app.queue import app as queue_app
from app.services import build_diagnosis
from app.services.build_diagnosis import (
    Diagnosis,
    JobLog,
    RunNotCompleted,
    diagnose,
    resolve_issues,
)


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
        patch("app.services.build_diagnosis.get_db", get_test_db),
        patch.object(settings, "ff_build_diagnosis", True),
        patch.object(settings, "neuralwatt_api_key", "test"),
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


async def pending_row(db_session_maker) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_session_maker() as db:
        db.add(
            Pipeline(
                id=pipeline_id,
                app_id="org.test.App",
                status=PipelineStatus.FAILED,
                params={"repo": "flathub/org.test.App", "sha": "a" * 40},
                provider_data={
                    "owner": "flathub-infra",
                    "repo": "vorarbeiter",
                    "run_id": "1",
                },
                callback_token="token",
            )
        )
        db.add(BuildDiagnosis(pipeline_id=pipeline_id))
        await db.commit()
    return pipeline_id


async def get_row(db_session_maker, pipeline_id: uuid.UUID) -> BuildDiagnosis:
    async with db_session_maker() as db:
        row = await db.get(BuildDiagnosis, pipeline_id)
    assert row is not None
    return row


LOG = "\n".join(
    f"2026-11-15T19:00:{i:02d}.0000000Z line {i}" for i in range(1, 41)
).encode()


def mock_github(run_status: str = "completed"):
    async def download(self, owner, repo, job_id, dest, max_bytes):
        dest.write_bytes(LOG)
        return len(LOG)

    return patch.multiple(
        "app.services.build_diagnosis.GitHubActionsService",
        get_workflow_run_details=AsyncMock(return_value={"status": run_status}),
        get_workflow_run_jobs=AsyncMock(
            return_value=[
                {
                    "id": 7,
                    "name": "build-x86_64",
                    "conclusion": "failure",
                    "steps": [
                        {
                            "name": "Build",
                            "conclusion": "failure",
                            "started_at": "2026-11-15T19:00:10Z",
                            "completed_at": "2026-11-15T19:00:30Z",
                        }
                    ],
                }
            ]
        ),
        download_job_log=download,
    )


@pytest.mark.asyncio
async def test_incomplete_run_retries_then_fails(db_session_maker, connector):
    pipeline_id = await pending_row(db_session_maker)

    with mock_github(run_status="in_progress"):
        with pytest.raises(RunNotCompleted):
            await diagnose(pipeline_id, attempts=0, rate_limit_retries=0)
        assert (
            await get_row(db_session_maker, pipeline_id)
        ).status == BuildDiagnosisStatus.PENDING

        await diagnose(
            pipeline_id,
            attempts=build_diagnosis.MAX_ATTEMPTS - 1,
            rate_limit_retries=0,
        )

    row = await get_row(db_session_maker, pipeline_id)
    assert row.status == BuildDiagnosisStatus.FAILED
    assert row.error is not None and row.error.startswith("gave up after 3 attempts")


@pytest.mark.asyncio
async def test_unexpected_exception_marks_failed(db_session_maker, connector):
    pipeline_id = await pending_row(db_session_maker)

    with patch(
        "app.services.build_diagnosis.load_jobs",
        AsyncMock(side_effect=KeyError("steps")),
    ):
        await diagnose(pipeline_id, attempts=0, rate_limit_retries=0)

    row = await get_row(db_session_maker, pipeline_id)
    assert row.status == BuildDiagnosisStatus.FAILED
    assert row.error == "KeyError('steps')"


@pytest.mark.asyncio
async def test_rate_limit_reschedules_without_using_attempts(
    db_session_maker, connector
):
    pipeline_id = await pending_row(db_session_maker)
    error = ModelRateLimitError("model", "429", retry_after=600)

    with (
        mock_github(),
        patch.object(build_diagnosis.agent, "run", AsyncMock(side_effect=error)),
    ):
        await diagnose(
            pipeline_id,
            attempts=build_diagnosis.MAX_ATTEMPTS - 1,
            rate_limit_retries=0,
        )
        jobs = list(connector.jobs.values())
        assert [j["args"] for j in jobs] == [
            {"pipeline_id": str(pipeline_id), "rate_limit_retries": 1}
        ]
        assert (
            await get_row(db_session_maker, pipeline_id)
        ).status == BuildDiagnosisStatus.PENDING

        await diagnose(
            pipeline_id,
            attempts=0,
            rate_limit_retries=build_diagnosis.RATE_LIMIT_RETRIES,
        )

    assert len(connector.jobs) == 1
    row = await get_row(db_session_maker, pipeline_id)
    assert row.status == BuildDiagnosisStatus.FAILED


@pytest.mark.asyncio
async def test_diagnosis_stores_resolved_evidence(db_session_maker, connector):
    pipeline_id = await pending_row(db_session_maker)
    output = {
        "issues": [
            {
                "category": "compile-error",
                "summary": "The wyrmsun patch did not apply.",
                "evidence": [{"job": 1, "start_line": 20, "end_line": 21}],
            }
        ]
    }

    with (
        mock_github(),
        build_diagnosis.agent.override(
            model=TestModel(call_tools=[], custom_output_args=output)
        ),
    ):
        await diagnose(pipeline_id, attempts=0, rate_limit_retries=0)

    row = await get_row(db_session_maker, pipeline_id)
    assert row.status == BuildDiagnosisStatus.COMPLETED
    assert row.result is not None
    assert row.result["issues"][0]["evidence"][0]["text"] == (
        "2026-11-15T19:00:20.0000000Z line 20\n2026-11-15T19:00:21.0000000Z line 21"
    )
    assert row.prompt_version == build_diagnosis.PROMPT_VERSION


def test_out_of_range_citation_drops_issue():
    log = JobLog(
        name="build", lines=["a"] * 100, failed_steps=["Build"], start=1, end=100
    )
    diagnosis = Diagnosis.model_validate(
        {
            "issues": [
                {
                    "category": "compile-error",
                    "summary": "past the end",
                    "evidence": [{"job": 1, "start_line": 99, "end_line": 101}],
                },
                {
                    "category": "linter",
                    "summary": "too long",
                    "evidence": [{"job": 1, "start_line": 1, "end_line": 16}],
                },
                {
                    "category": "linter",
                    "summary": "unknown job",
                    "evidence": [{"job": 2, "start_line": 1, "end_line": 1}],
                },
                {
                    "category": "unknown",
                    "summary": "valid",
                    "evidence": [{"job": 1, "start_line": 1, "end_line": 15}],
                },
            ]
        }
    )

    assert [i["summary"] for i in resolve_issues(diagnosis, [log])] == ["valid"]
