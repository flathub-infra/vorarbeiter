from django.utils import timezone
from django.db import models
import logging

from .models import PipelineInstance, JobInstance

logger = logging.getLogger(__name__)


class PipelineRunner:
    @classmethod
    async def start_pipeline(cls, pipeline_instance_id):
        pipeline = PipelineInstance.objects.get(id=pipeline_instance_id)
        pipeline.status = PipelineInstance.Status.RUNNING
        pipeline.started_at = timezone.now()
        pipeline.save()

        root_jobs = pipeline.job_instances.filter(job_template__depends_on__isnull=True)

        for job in root_jobs:
            await cls.start_job(job.id)

    @classmethod
    async def start_job(cls, job_instance_id):
        job = JobInstance.objects.get(id=job_instance_id)

        job.status = JobInstance.Status.QUEUED
        job.queued_at = timezone.now()
        job.save()

        provider_impl = job.job_template.provider.get_implementation()

        try:
            external_job_id = await provider_impl.dispatch_job(job)

            job.external_job_id = external_job_id
            job.status = JobInstance.Status.RUNNING
            job.started_at = timezone.now()
            job.save()

            return True
        except Exception as e:
            logger.exception(f"Failed to dispatch job {job.id}: {str(e)}")

            job.status = JobInstance.Status.FAILED
            job.results = {"error": str(e)}
            job.finished_at = timezone.now()
            job.save()

            cls._check_pipeline_status(job.pipeline_instance)

            return False

    @classmethod
    async def check_and_start_dependent_jobs(cls, completed_job):
        if completed_job.status != JobInstance.Status.SUCCEEDED:
            return

        pipeline = completed_job.pipeline_instance
        job_template = completed_job.job_template
        dependent_templates = job_template.required_by.all()

        for dep_template in dependent_templates:
            try:
                dep_job = JobInstance.objects.get(
                    pipeline_instance=pipeline, job_template=dep_template
                )

                dependencies_satisfied = True
                for dep in dep_template.depends_on.all():
                    try:
                        dep_instance = JobInstance.objects.get(
                            pipeline_instance=pipeline, job_template=dep
                        )
                        if dep_instance.status != JobInstance.Status.SUCCEEDED:
                            dependencies_satisfied = False
                            break
                    except JobInstance.DoesNotExist:
                        dependencies_satisfied = False
                        break

                if (
                    dependencies_satisfied
                    and dep_job.status == JobInstance.Status.PENDING
                ):
                    await cls.start_job(dep_job.id)

            except JobInstance.DoesNotExist:
                continue

    @classmethod
    def _check_pipeline_status(cls, pipeline):
        job_counts = pipeline.job_instances.values("status").annotate(
            count=models.Count("id")
        )
        status_counts = {item["status"]: item["count"] for item in job_counts}

        total_jobs = pipeline.job_instances.count()
        failed_jobs = status_counts.get(JobInstance.Status.FAILED, 0)
        cancelled_jobs = status_counts.get(JobInstance.Status.CANCELLED, 0)
        succeeded_jobs = status_counts.get(JobInstance.Status.SUCCEEDED, 0)

        if failed_jobs > 0:
            pipeline.status = PipelineInstance.Status.FAILED
            pipeline.finished_at = timezone.now()
            pipeline.save()
        elif (
            cancelled_jobs > 0
            and (succeeded_jobs + failed_jobs + cancelled_jobs) == total_jobs
        ):
            pipeline.status = PipelineInstance.Status.CANCELLED
            pipeline.finished_at = timezone.now()
            pipeline.save()
        elif succeeded_jobs == total_jobs:
            pipeline.status = PipelineInstance.Status.SUCCEEDED
            pipeline.finished_at = timezone.now()
            pipeline.save()
