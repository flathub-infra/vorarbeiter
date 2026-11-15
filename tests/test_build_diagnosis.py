import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from procrastinate.testing import InMemoryConnector
from pydantic_ai.exceptions import ModelRateLimitError
from pydantic_ai.models.decision import ChoiceAnswer, DecisionResponse
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from app.config import settings
from app.main import app as main_app
from app.models import BuildDiagnosis, BuildDiagnosisStatus, Pipeline, PipelineStatus
from app.pipelines.build import BuildPipeline
from app.queue import app as queue_app
from app.services import build_diagnosis
from app.services.build_diagnosis import (
    Diagnosis,
    JobLog,
    RunNotCompleted,
    comment_body,
    diagnose,
    post_comment,
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
        patch.multiple(
            settings,
            llm_url="https://llm.example/v1",
            llm_api_key="test",
            llm_model="llm",
            decisions_url="https://decisions.example/v1",
            decisions_api_key="test",
            decisions_model="mammoth-1",
        ),
        patch(
            "app.pipelines.build.GitHubNotifier.handle_build_completion",
            AsyncMock(),
        ),
        queue_app.replace_connector(connector),
    ):
        yield connector


async def failure_callback(
    db_session_maker, infrastructure_cancellation: bool = False
) -> uuid.UUID:
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
    with patch(
        "app.services.github_actions.GitHubActionsService.check_run_was_cancelled",
        AsyncMock(return_value=infrastructure_cancellation),
    ):
        await build.handle_status_callback(pipeline_id, {"status": "failure"})
    return pipeline_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("infrastructure_cancellation", "status"),
    [(False, PipelineStatus.FAILED), (True, PipelineStatus.CANCELLED)],
)
async def test_failure_defers_one_job(
    db_session_maker, connector, infrastructure_cancellation, status
):
    pipeline_id = await failure_callback(db_session_maker, infrastructure_cancellation)

    jobs = list(connector.jobs.values())
    assert [(j["task_name"], j["queue_name"]) for j in jobs] == [
        ("app.tasks.diagnose_build", "build-diagnosis")
    ]
    assert jobs[0]["args"] == {"pipeline_id": str(pipeline_id)}
    async with db_session_maker() as db:
        assert await db.get(BuildDiagnosis, pipeline_id) is not None
        pipeline = await db.get(Pipeline, pipeline_id)
    assert pipeline is not None
    assert pipeline.status == status


@pytest.mark.asyncio
async def test_repeated_callback_defers_none(db_session_maker, connector):
    pipeline_id = await failure_callback(db_session_maker)
    build = BuildPipeline()
    build.start_pending_builds = AsyncMock(return_value=[])

    with patch(
        "app.services.github_actions.GitHubActionsService.check_run_was_cancelled",
        AsyncMock(return_value=False),
    ):
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


@contextmanager
def mock_github(run_status: str = "completed", annotations: tuple[str, ...] = ()):
    async def download(self, owner, repo, job_id, dest, max_bytes):
        dest.write_bytes(LOG)
        return len(LOG)

    with (
        patch.multiple(
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
        ),
        patch(
            "app.services.build_diagnosis.get_check_run_annotations",
            AsyncMock(return_value=[{"message": m} for m in annotations]),
        ),
    ):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("annotations", "summary"),
    [
        (
            (
                "Process completed with exit code 137.",
                "AWS interrupted the EC2 Spot instance running this job.",
            ),
            build_diagnosis.SPOT_SUMMARY,
        ),
        (
            ("The job was not acquired by Runner of type hosted even after multiple",),
            build_diagnosis.GITHUB_CANCELLED_SUMMARY,
        ),
    ],
)
async def test_cancelled_run_is_reported_without_models(
    db_session_maker, connector, annotations, summary
):
    pipeline_id = await pending_row(db_session_maker)
    async with db_session_maker() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        assert pipeline is not None
        pipeline.params = {
            **pipeline.params,
            "pr_number": "5",
            "ref": "refs/pull/5/head",
        }
        await db.commit()
    run = AsyncMock()
    ensure = AsyncMock(return_value=True)

    with (
        mock_github(annotations=annotations),
        patch.object(build_diagnosis.agent, "run", run),
        patch.object(settings, "ff_build_diagnosis_comments", True),
        patch("app.services.build_diagnosis.ensure_pr_comment", ensure),
    ):
        await diagnose(pipeline_id, attempts=0, rate_limit_retries=0)

    row = await get_row(db_session_maker, pipeline_id)
    assert row.status == BuildDiagnosisStatus.COMPLETED
    assert row.result == {
        "issues": [{"category": "cancelled", "summary": summary, "evidence": []}]
    }
    run.assert_not_awaited()
    body = ensure.await_args_list[0].args[3]
    assert summary in body
    assert row.comment_posted_at is not None


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
                "summary": "The wyrmsun patch did not apply.",
                "evidence": [{"job": 1, "start_line": 20, "end_line": 21}],
            }
        ]
    }

    decision = DecisionResponse(
        answers={
            "category": ChoiceAnswer(
                choice="compile-error",
                confidence=0.9,
                probabilities={"compile-error": 0.95, "unknown": 0.05},
            )
        },
        model_name="mammoth-1",
    )
    decide = AsyncMock(return_value=decision)

    with (
        mock_github(),
        build_diagnosis.agent.override(
            model=TestModel(call_tools=[], custom_output_args=output)
        ),
        patch.object(build_diagnosis.SystemOneModel, "decide", decide),
    ):
        await diagnose(pipeline_id, attempts=0, rate_limit_retries=0)

    row = await get_row(db_session_maker, pipeline_id)
    assert row.status == BuildDiagnosisStatus.COMPLETED
    assert row.result is not None
    issue = row.result["issues"][0]
    assert issue["evidence"][0]["text"] == (
        "2026-11-15T19:00:20.0000000Z line 20\n2026-11-15T19:00:21.0000000Z line 21"
    )
    assert issue["category"] == "compile-error"
    request = decide.await_args_list[0].args[0]
    assert request.state == {
        "diagnosis": "The wyrmsun patch did not apply.",
        "evidence": [issue["evidence"][0]["text"]],
    }


def test_out_of_range_citation_drops_issue():
    log = JobLog(
        name="build", lines=["a"] * 100, failed_steps=["Build"], start=1, end=100
    )
    diagnosis = Diagnosis.model_validate(
        {
            "issues": [
                {
                    "summary": "past the end",
                    "evidence": [{"job": 1, "start_line": 99, "end_line": 101}],
                },
                {
                    "summary": "too long",
                    "evidence": [{"job": 1, "start_line": 1, "end_line": 16}],
                },
                {
                    "summary": "unknown job",
                    "evidence": [{"job": 2, "start_line": 1, "end_line": 1}],
                },
                {
                    "summary": "valid",
                    "evidence": [{"job": 1, "start_line": 1, "end_line": 15}],
                },
            ]
        }
    )

    assert [i["summary"] for i in resolve_issues(diagnosis, [log])] == ["valid"]


ISSUE = {
    "summary": "See https://example.com @flathub/admins **now**",
    "category": "compile-error",
    "evidence": [
        {
            "job_name": "build-x86_64",
            "start_line": 20,
            "end_line": 21,
            "text": "2026-11-15T19:00:20.0000000Z ```\n2026-11-15T19:00:21.0000000Z boom",
        }
    ],
}


def test_comment_ends_with_marker_and_strips_markup():
    pipeline_id = uuid.uuid4()

    body = comment_body(pipeline_id, [ISSUE])

    assert body.endswith(f"<!-- vorarbeiter:build-diagnosis:{pipeline_id} -->")
    assert "See flathub/admins now" in body
    assert "````\n```\nboom\n````" in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "newer", "posted"),
    [
        ("compile-error", False, True),
        ("unknown", False, False),
        ("linter", True, False),
    ],
)
async def test_comment_gates(db_session_maker, connector, category, newer, posted):
    pipeline_id = await pending_row(db_session_maker)
    async with db_session_maker() as db:
        pipeline = await db.get(Pipeline, pipeline_id)
        assert pipeline is not None
        pipeline.params = {
            **pipeline.params,
            "pr_number": "5",
            "ref": "refs/pull/5/head",
        }
        if newer:
            db.add(
                Pipeline(
                    app_id="org.test.App",
                    status=PipelineStatus.PENDING,
                    params=pipeline.params,
                    created_at=pipeline.created_at + timedelta(minutes=1),
                    callback_token="token2",
                )
            )
        await db.commit()
    ensure = AsyncMock(return_value=True)

    with (
        patch.object(settings, "ff_build_diagnosis_comments", True),
        patch("app.services.build_diagnosis.ensure_pr_comment", ensure),
    ):
        await post_comment(pipeline, [{**ISSUE, "category": category}])

    assert ensure.await_count == int(posted)
    row = await get_row(db_session_maker, pipeline_id)
    assert (row.comment_posted_at is not None) == posted


@pytest.mark.asyncio
async def test_diagnosis_endpoint(db_session_maker):
    pipeline_id = await pending_row(db_session_maker)

    @asynccontextmanager
    async def get_test_db(*, use_replica: bool = False):
        async with db_session_maker() as db:
            yield db

    with patch("app.routes.pipelines.get_db", get_test_db):
        client = TestClient(main_app)
        found = client.get(f"/api/pipelines/{pipeline_id}/diagnosis")
        missing = client.get(f"/api/pipelines/{uuid.uuid4()}/diagnosis")

    assert found.status_code == 200
    assert found.json()["status"] == "pending"
    assert missing.status_code == 404
