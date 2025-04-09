from datetime import datetime

from app.database import get_db
from app.models import Pipeline, PipelineStatus, Job, JobStatus
from app.providers import ProviderType, ProviderFactory


async def example_pipeline_workflow():
    async with get_db() as db:
        pipeline = Pipeline(
            app_id="org.flathub.Example",
            status=PipelineStatus.PENDING,
            params={"commit": "abcdef1234567890", "repo": "stable", "branch": "main"},
            created_at=datetime.now(),
        )
        db.add(pipeline)
        await db.flush()

        build_job = Job(
            pipeline_id=pipeline.id,
            job_type="build",
            status=JobStatus.PENDING,
            provider=ProviderType.GITHUB.value,
            provider_data={},
            position=1,
            created_at=datetime.now(),
        )
        db.add(build_job)

        test_job = Job(
            pipeline_id=pipeline.id,
            job_type="test",
            status=JobStatus.PENDING,
            provider=ProviderType.GITHUB.value,
            provider_data={},
            position=2,
            created_at=datetime.now(),
        )
        db.add(test_job)

        publish_job = Job(
            pipeline_id=pipeline.id,
            job_type="publish",
            status=JobStatus.PENDING,
            provider=ProviderType.GITHUB.value,
            provider_data={},
            position=3,
            created_at=datetime.now(),
        )
        db.add(publish_job)

        await db.flush()

        pipeline.status = PipelineStatus.RUNNING
        pipeline.started_at = datetime.now()

        build_job.status = JobStatus.RUNNING
        build_job.started_at = datetime.now()

        github_config = {
            "token": "github_token_here",
            "base_url": "https://api.github.com",
        }

        github_provider = await ProviderFactory.create_provider(
            ProviderType.GITHUB, github_config
        )

        job_data = {
            "app_id": pipeline.app_id,
            "job_type": build_job.job_type,
            "params": {
                "owner": "flathub",
                "repo": "actions",
                "workflow_id": "build.yml",
                "ref": "main",
                "inputs": {
                    "flatpak_id": pipeline.app_id,
                    "commit": pipeline.params["commit"],
                },
            },
        }

        provider_result = await github_provider.dispatch(
            str(build_job.id), str(pipeline.id), job_data
        )

        build_job.provider_data = provider_result

        await db.commit()

        build_job.status = JobStatus.COMPLETE
        build_job.finished_at = datetime.now()
        build_job.result = {
            "success": True,
            "build_id": "123456",
            "logs_url": "https://github.com/flathub/actions/runs/123456",
        }

        test_job.status = JobStatus.RUNNING
        test_job.started_at = datetime.now()

        test_job_data = {
            "app_id": pipeline.app_id,
            "job_type": test_job.job_type,
            "params": {
                "owner": "flathub",
                "repo": "actions",
                "workflow_id": "test.yml",
                "ref": "main",
                "inputs": {
                    "flatpak_id": pipeline.app_id,
                    "build_id": build_job.result["build_id"],
                },
            },
        }

        test_provider_result = await github_provider.dispatch(
            str(test_job.id), str(pipeline.id), test_job_data
        )

        test_job.provider_data = test_provider_result

        await db.commit()
