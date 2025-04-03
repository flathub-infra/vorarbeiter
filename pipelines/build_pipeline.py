from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import logging
from django.utils import timezone
import uuid

from .models import PipelineInstance, JobInstance
from .providers.github_actions import GitHubActionsProvider

logger = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    workflow: str
    ref: str = "main"
    inputs: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)


@dataclass
class BuildPipeline:
    name: str
    description: str
    jobs: Dict[str, Job]
    fail_fast: bool = True


class BuildPipelineRegistry:
    _pipelines: Dict[str, BuildPipeline] = {}

    @classmethod
    def register(cls, pipeline: BuildPipeline):
        cls._pipelines[pipeline.name] = pipeline
        return pipeline

    @classmethod
    def get_pipeline(cls, name: str) -> Optional[BuildPipeline]:
        return cls._pipelines.get(name)

    @classmethod
    def get_all_pipelines(cls) -> Dict[str, BuildPipeline]:
        return cls._pipelines


class BuildPipelineRunner:
    @classmethod
    async def create_and_run_pipeline(
        cls,
        pipeline_name: str,
        trigger_params: Optional[Dict[str, Any]] = None,
        github_provider_id: Optional[int] = None,
    ) -> PipelineInstance:
        pipeline_def = BuildPipelineRegistry.get_pipeline(pipeline_name)
        if not pipeline_def:
            raise ValueError(f"Pipeline {pipeline_name} not found")

        pipeline_instance = PipelineInstance.objects.create(
            trigger_parameters={
                "pipeline_name": pipeline_name,
                **(trigger_params or {}),
            },
            status=PipelineInstance.Status.RUNNING,
            started_at=timezone.now(),
        )

        job_instances = {}
        for job_name, job_def in pipeline_def.jobs.items():
            job_instance = JobInstance.objects.create(
                pipeline_instance=pipeline_instance,
                status=JobInstance.Status.PENDING,
                callback_id=uuid.uuid4(),
                execution_parameters={
                    "job_name": job_name,
                    "workflow": job_def.workflow,
                    "ref": job_def.ref,
                    "inputs": job_def.inputs,
                    **(trigger_params or {}),
                },
            )
            job_instances[job_name] = job_instance

        root_jobs = [
            job_name
            for job_name, job_def in pipeline_def.jobs.items()
            if not job_def.depends_on
        ]

        for job_name in root_jobs:
            await cls._execute_job(
                job_instances[job_name], pipeline_def, job_instances, github_provider_id
            )

        return pipeline_instance

    @classmethod
    async def _execute_job(
        cls,
        job_instance: JobInstance,
        pipeline_def: BuildPipeline,
        all_job_instances: Dict[str, JobInstance],
        github_provider_id: Optional[int] = None,
    ):
        from .models import Provider

        job_name = job_instance.execution_parameters["job_name"]
        pipeline_def.jobs[job_name]

        job_instance.status = JobInstance.Status.QUEUED
        job_instance.queued_at = timezone.now()
        job_instance.save()

        provider = None
        if github_provider_id:
            provider = Provider.objects.get(id=github_provider_id)
        else:
            provider = Provider.objects.filter(provider_type="github_actions").first()

        if not provider:
            job_instance.status = JobInstance.Status.FAILED
            job_instance.results = {"error": "No GitHub Actions provider found"}
            job_instance.finished_at = timezone.now()
            job_instance.save()
            await cls._update_pipeline_status(all_job_instances, pipeline_def)
            return

        provider_impl = GitHubActionsProvider(provider)

        try:
            job_instance.external_job_id = None
            job_instance.save()

            external_job_id = await provider_impl.dispatch_job(job_instance)

            job_instance.external_job_id = external_job_id
            job_instance.status = JobInstance.Status.RUNNING
            job_instance.started_at = timezone.now()
            job_instance.save()

        except Exception as e:
            logger.exception(f"Failed to dispatch job {job_name}: {str(e)}")
            job_instance.status = JobInstance.Status.FAILED
            job_instance.results = {"error": str(e)}
            job_instance.finished_at = timezone.now()
            job_instance.save()

            if pipeline_def.fail_fast:
                await cls._mark_dependent_jobs_as_skipped(
                    job_name, all_job_instances, pipeline_def
                )

            await cls._update_pipeline_status(all_job_instances, pipeline_def)

    @classmethod
    async def _mark_dependent_jobs_as_skipped(
        cls,
        failed_job: str,
        all_job_instances: Dict[str, JobInstance],
        pipeline_def: BuildPipeline,
    ):
        for job_name, job_def in pipeline_def.jobs.items():
            job_instance = all_job_instances[job_name]
            if job_instance.status not in [
                JobInstance.Status.PENDING,
                JobInstance.Status.QUEUED,
            ]:
                continue

            if failed_job in job_def.depends_on:
                job_instance.status = JobInstance.Status.SKIPPED
                job_instance.finished_at = timezone.now()
                job_instance.save()

                await cls._mark_dependent_jobs_as_skipped(
                    job_name, all_job_instances, pipeline_def
                )

    @classmethod
    async def handle_job_callback(
        cls,
        job_instance: JobInstance,
        status: str,
        result: Optional[Dict[str, Any]] = None,
    ):
        job_instance.status = status
        job_instance.results = result or {}
        job_instance.finished_at = timezone.now()
        job_instance.save()

        all_job_instances = {
            ji.execution_parameters.get("job_name", f"job_{ji.id}"): ji
            for ji in JobInstance.objects.filter(
                pipeline_instance=job_instance.pipeline_instance
            )
        }

        pipeline_name = job_instance.pipeline_instance.trigger_parameters.get(
            "pipeline_name"
        )
        pipeline_def = BuildPipelineRegistry.get_pipeline(pipeline_name)
        if not pipeline_def:
            logger.error(f"Pipeline {pipeline_name} not found in registry")
            return

        if status == JobInstance.Status.FAILED and pipeline_def.fail_fast:
            current_job_name = job_instance.execution_parameters.get("job_name")
            if current_job_name:
                await cls._mark_dependent_jobs_as_skipped(
                    current_job_name, all_job_instances, pipeline_def
                )

        elif status == JobInstance.Status.SUCCEEDED:
            current_job_name = job_instance.execution_parameters.get("job_name")
            if current_job_name:
                await cls._check_and_start_dependent_jobs(
                    current_job_name,
                    all_job_instances,
                    pipeline_def,
                    job_instance.pipeline_instance.id,
                )

        await cls._update_pipeline_status(all_job_instances, pipeline_def)

    @classmethod
    async def _check_and_start_dependent_jobs(
        cls,
        completed_job: str,
        all_job_instances: Dict[str, JobInstance],
        pipeline_def: BuildPipeline,
        pipeline_instance_id: int,
    ):
        from .models import Provider

        for job_name, job_def in pipeline_def.jobs.items():
            if completed_job in job_def.depends_on:
                job_instance = all_job_instances.get(job_name)
                if (
                    not job_instance
                    or job_instance.status != JobInstance.Status.PENDING
                ):
                    continue

                dependencies_satisfied = True
                for dep_name in job_def.depends_on:
                    dep_instance = all_job_instances.get(dep_name)
                    if (
                        not dep_instance
                        or dep_instance.status != JobInstance.Status.SUCCEEDED
                    ):
                        dependencies_satisfied = False
                        break

                if dependencies_satisfied:
                    provider = Provider.objects.filter(
                        provider_type="github_actions"
                    ).first()
                    if not provider:
                        logger.error("No GitHub Actions provider found")
                        job_instance.status = JobInstance.Status.FAILED
                        job_instance.results = {
                            "error": "No GitHub Actions provider found"
                        }
                        job_instance.finished_at = timezone.now()
                        job_instance.save()
                        continue

                    await cls._execute_job(
                        job_instance, pipeline_def, all_job_instances, provider.id
                    )

    @classmethod
    async def _update_pipeline_status(
        cls, all_job_instances: Dict[str, JobInstance], pipeline_def: BuildPipeline
    ):
        statuses = [ji.status for ji in all_job_instances.values()]

        if not all_job_instances:
            return

        first_job = next(iter(all_job_instances.values()))
        pipeline = first_job.pipeline_instance

        # Don't update if pipeline is already in a final state
        if pipeline.status == PipelineInstance.Status.PUBLISHED:
            return

        if JobInstance.Status.FAILED in statuses:
            pipeline.status = PipelineInstance.Status.FAILED
            pipeline.finished_at = timezone.now()
            pipeline.save()
            return

        pending_statuses = [
            JobInstance.Status.PENDING,
            JobInstance.Status.QUEUED,
            JobInstance.Status.RUNNING,
        ]

        all_completed = not any(status in pending_statuses for status in statuses)

        if all_completed:
            if all(
                status == JobInstance.Status.SUCCEEDED
                for status in statuses
                if status != JobInstance.Status.SKIPPED
            ):
                pipeline.status = PipelineInstance.Status.SUCCEEDED
            else:
                pipeline.status = PipelineInstance.Status.CANCELLED

            pipeline.finished_at = timezone.now()
            pipeline.save()


build_pipeline = BuildPipelineRegistry.register(
    BuildPipeline(
        name="build",
        description="Standard Flathub build pipeline",
        fail_fast=True,
        jobs={
            "validate_manifest": Job(
                name="Validate manifest",
                workflow="validate_manifest.yml",
                inputs={},
                depends_on=[],
            ),
            "create_build": Job(
                name="Create build",
                workflow="create_build.yml",
                inputs={},
                depends_on=["validate_manifest"],
            ),
            "build_x86_64": Job(
                name="Build x86_64",
                workflow="build_x86_64.yml",
                inputs={},
                depends_on=["create_build"],
            ),
            "build_aarch64": Job(
                name="Build aarch64",
                workflow="build_aarch64.yml",
                inputs={},
                depends_on=["create_build"],
            ),
            # The publish job will be triggered by the publish_ready_pipelines command
            # after the pipeline has been in SUCCEEDED status for 1 hour
            "publish": Job(
                name="Publish",
                workflow="publish.yml",
                inputs={},
                depends_on=["build_x86_64", "build_aarch64"],
            ),
        },
    )
)
