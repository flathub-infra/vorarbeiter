import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.models.job import Job, JobStatus


@pytest.fixture
def sample_pipeline():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.RUNNING,
        params={"branch": "main"},
        triggered_by=PipelineTrigger.MANUAL,
        created_at=datetime.now(),
    )


def test_job_creation_minimal():
    job = Job(
        job_type="build",
        position=1,
        provider_data={"workflow": "build.yml"},
        pipeline_id=uuid.uuid4(),
        status=JobStatus.PENDING,
    )

    assert job.status == JobStatus.PENDING
    assert job.job_type == "build"
    assert job.position == 1
    assert job.provider_data == {"workflow": "build.yml"}
    assert job.result is None
    assert job.started_at is None
    assert job.finished_at is None


def test_job_creation_with_all_fields():
    job_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    now = datetime.now()

    job = Job(
        id=job_id,
        status=JobStatus.RUNNING,
        job_type="test",
        position=2,
        provider_data={"run_id": 12345},
        result={"output": "Success", "exit_code": 0},
        created_at=now,
        started_at=now,
        finished_at=None,
        pipeline_id=pipeline_id,
    )

    assert job.id == job_id
    assert job.status == JobStatus.RUNNING
    assert job.job_type == "test"
    assert job.position == 2
    assert job.provider_data == {"run_id": 12345}
    assert job.result == {"output": "Success", "exit_code": 0}
    assert job.created_at == now
    assert job.started_at == now
    assert job.finished_at is None
    assert job.pipeline_id == pipeline_id


def test_job_status_enum_values():
    assert JobStatus.PENDING.value == "pending"
    assert JobStatus.RUNNING.value == "running"
    assert JobStatus.COMPLETE.value == "complete"
    assert JobStatus.FAILED.value == "failed"
    assert JobStatus.CANCELLED.value == "cancelled"


def test_job_repr():
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        status=JobStatus.RUNNING,
        job_type="build",
        position=1,
        provider_data={},
        pipeline_id=uuid.uuid4(),
    )

    repr_str = repr(job)
    assert f"<Job(id={job_id}" in repr_str
    assert "type='build'" in repr_str
    assert "status='RUNNING'" in repr_str


def test_job_with_complex_provider_data():
    provider_data = {
        "workflow_id": "build.yml",
        "run_id": 123456,
        "inputs": {"app_id": "org.test.App", "branch": "main", "commit": "abc123"},
        "environment": {"FLATPAK_ID": "org.test.App", "BUILD_TYPE": "release"},
    }

    job = Job(
        job_type="build",
        position=1,
        provider_data=provider_data,
        pipeline_id=uuid.uuid4(),
    )

    assert job.provider_data == provider_data
    assert job.provider_data["inputs"]["app_id"] == "org.test.App"
    assert job.provider_data["environment"]["BUILD_TYPE"] == "release"


def test_job_with_complex_result():
    result = {
        "status": "success",
        "output": {
            "logs": ["Step 1 complete", "Step 2 complete"],
            "artifacts": [
                {"name": "build.log", "size": 1024},
                {"name": "app.flatpak", "size": 10485760},
            ],
        },
        "metrics": {"duration_seconds": 300, "cpu_usage": 85.5, "memory_mb": 2048},
    }

    job = Job(
        job_type="build",
        position=1,
        provider_data={},
        result=result,
        pipeline_id=uuid.uuid4(),
    )

    assert job.result == result
    assert job.result["status"] == "success"
    assert len(job.result["output"]["artifacts"]) == 2
    assert job.result["metrics"]["duration_seconds"] == 300


def test_job_status_transitions():
    job = Job(
        job_type="test",
        position=1,
        provider_data={},
        pipeline_id=uuid.uuid4(),
        status=JobStatus.PENDING,
    )

    assert job.status == JobStatus.PENDING

    job.status = JobStatus.RUNNING
    job.started_at = datetime.now()
    assert job.status == JobStatus.RUNNING
    assert job.started_at is not None

    job.status = JobStatus.COMPLETE
    job.finished_at = datetime.now()
    job.result = {"status": "success"}
    assert job.status == JobStatus.COMPLETE
    assert job.finished_at is not None
    assert job.result["status"] == "success"


def test_job_failed_status():
    job = Job(
        job_type="build",
        position=1,
        provider_data={},
        status=JobStatus.FAILED,
        result={"error": "Build failed", "exit_code": 1},
        pipeline_id=uuid.uuid4(),
    )

    assert job.status == JobStatus.FAILED
    assert job.result["error"] == "Build failed"
    assert job.result["exit_code"] == 1


def test_job_cancelled_status():
    job = Job(
        job_type="test",
        position=2,
        provider_data={},
        status=JobStatus.CANCELLED,
        result={"reason": "User cancelled"},
        pipeline_id=uuid.uuid4(),
    )

    assert job.status == JobStatus.CANCELLED
    assert job.result["reason"] == "User cancelled"


@pytest.mark.asyncio
async def test_job_pipeline_relationship(db_session_maker, sample_pipeline):
    async with db_session_maker() as session:
        session.add(sample_pipeline)
        await session.flush()

        job1 = Job(
            job_type="build",
            position=1,
            provider_data={"step": "compile"},
            pipeline_id=sample_pipeline.id,
        )
        job2 = Job(
            job_type="test",
            position=2,
            provider_data={"step": "unit-tests"},
            pipeline_id=sample_pipeline.id,
        )

        session.add(job1)
        session.add(job2)
        await session.commit()

        result = await session.execute(
            select(Job)
            .where(Job.pipeline_id == sample_pipeline.id)
            .order_by(Job.position)
        )
        jobs = result.scalars().all()

        assert len(jobs) == 2
        assert jobs[0].job_type == "build"
        assert jobs[0].position == 1
        assert jobs[1].job_type == "test"
        assert jobs[1].position == 2


@pytest.mark.asyncio
async def test_job_persistence(db_session_maker):
    pipeline = Pipeline(
        app_id="org.test.PersistApp",
        status=PipelineStatus.RUNNING,
        params={},
        triggered_by=PipelineTrigger.WEBHOOK,
    )

    async with db_session_maker() as session:
        session.add(pipeline)
        await session.flush()

        job = Job(
            job_type="deploy",
            position=1,
            provider_data={"environment": "production"},
            status=JobStatus.RUNNING,
            started_at=datetime.now(),
            pipeline_id=pipeline.id,
        )

        session.add(job)
        await session.commit()

        job_id = job.id

    async with db_session_maker() as session:
        loaded_job = await session.get(Job, job_id)

        assert loaded_job is not None
        assert loaded_job.job_type == "deploy"
        assert loaded_job.position == 1
        assert loaded_job.provider_data == {"environment": "production"}
        assert loaded_job.status == JobStatus.RUNNING
        assert loaded_job.started_at is not None


@pytest.mark.asyncio
async def test_job_query_by_status(db_session_maker, sample_pipeline):
    async with db_session_maker() as session:
        session.add(sample_pipeline)
        await session.flush()

        jobs = [
            Job(
                job_type="build",
                position=1,
                provider_data={},
                status=JobStatus.COMPLETE,
                pipeline_id=sample_pipeline.id,
            ),
            Job(
                job_type="test",
                position=2,
                provider_data={},
                status=JobStatus.RUNNING,
                pipeline_id=sample_pipeline.id,
            ),
            Job(
                job_type="deploy",
                position=3,
                provider_data={},
                status=JobStatus.PENDING,
                pipeline_id=sample_pipeline.id,
            ),
        ]

        for job in jobs:
            session.add(job)
        await session.commit()

        result = await session.execute(
            select(Job).where(Job.status == JobStatus.RUNNING)
        )
        running_jobs = result.scalars().all()

        assert len(running_jobs) == 1
        assert running_jobs[0].job_type == "test"

        result = await session.execute(
            select(Job).where(Job.status == JobStatus.PENDING)
        )
        pending_jobs = result.scalars().all()

        assert len(pending_jobs) == 1
        assert pending_jobs[0].job_type == "deploy"


def test_job_with_null_result():
    job = Job(
        job_type="cleanup",
        position=99,
        provider_data={},
        result=None,
        pipeline_id=uuid.uuid4(),
    )

    assert job.result is None


def test_job_timestamps():
    now = datetime.now()
    later = datetime.now()

    job = Job(
        job_type="build",
        position=1,
        provider_data={},
        created_at=now,
        started_at=now,
        finished_at=later,
        pipeline_id=uuid.uuid4(),
    )

    assert job.created_at == now
    assert job.started_at == now
    assert job.finished_at == later
    assert job.finished_at >= job.started_at


@pytest.mark.asyncio
async def test_job_default_status_on_persist(db_session_maker, sample_pipeline):
    async with db_session_maker() as session:
        session.add(sample_pipeline)
        await session.flush()

        job = Job(
            job_type="build",
            position=1,
            provider_data={},
            pipeline_id=sample_pipeline.id,
        )

        session.add(job)
        await session.flush()

        assert job.status == JobStatus.PENDING
