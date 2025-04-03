import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from django.db import transaction

from pipelines.models import PipelineInstance, JobInstance
from pipelines.providers.github_actions import GitHubActionsProvider

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Publishes pipelines that succeeded at least 1 hour ago"

    def add_arguments(self, parser):
        parser.add_argument(
            "--delay-hours",
            type=float,
            default=1.0,
            help="Delay in hours before publishing a successful pipeline (default: 1.0)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only list the pipelines that would be published without actually publishing them",
        )

    def handle(self, *args, **options):
        delay_hours = options["delay_hours"]
        dry_run = options["dry_run"]

        # Calculate the cutoff time (1 hour ago by default)
        cutoff_time = timezone.now() - timedelta(hours=delay_hours)

        # Find pipelines that succeeded at least 1 hour ago
        ready_pipelines = PipelineInstance.objects.filter(
            status=PipelineInstance.Status.SUCCEEDED, finished_at__lt=cutoff_time
        )

        # Count the pipelines we found
        count = ready_pipelines.count()
        self.stdout.write(f"Found {count} pipeline(s) ready for publishing")

        if count == 0:
            return

        if dry_run:
            # Just list the pipelines
            for pipeline in ready_pipelines:
                self.stdout.write(
                    f"Would publish: Pipeline #{pipeline.id}, finished at {pipeline.finished_at}"
                )
            return

        # Actually publish them
        for pipeline in ready_pipelines:
            self.publish_pipeline(pipeline)

        self.stdout.write(
            self.style.SUCCESS(f"Successfully published {count} pipeline(s)")
        )

    def publish_pipeline(self, pipeline):
        """Publish a pipeline by running the publish job"""
        self.stdout.write(f"Publishing pipeline #{pipeline.id}...")

        # Check if this is a code-defined pipeline or DB-defined pipeline
        pipeline_name = pipeline.trigger_parameters.get("pipeline_name")

        if pipeline_name:
            # This is a code-defined pipeline
            self._publish_code_pipeline(pipeline, pipeline_name)
        else:
            # This is a database-defined pipeline
            self._publish_db_pipeline(pipeline)

    def _publish_code_pipeline(self, pipeline, pipeline_name):
        """Publish a code-defined pipeline"""
        from pipelines.models import Provider
        from pipelines.build_pipeline import BuildPipelineRegistry

        pipeline_def = BuildPipelineRegistry.get_pipeline(pipeline_name)
        if not pipeline_def:
            logger.error(f"Pipeline definition {pipeline_name} not found")
            return

        # Ensure there's a publish job in this pipeline
        if "publish" not in pipeline_def.jobs:
            logger.error(f"No publish job found in pipeline {pipeline_name}")
            return

        # Get the publish job definition
        publish_job_def = pipeline_def.jobs["publish"]

        # Find a provider
        provider = Provider.objects.filter(provider_type="github_actions").first()
        if not provider:
            logger.error("No GitHub Actions provider found")
            return

        # Create a job instance for the publish job
        with transaction.atomic():
            # Update pipeline status
            pipeline.status = PipelineInstance.Status.PUBLISHED
            pipeline.save()

            # Find or create a job instance for publishing
            try:
                # Try to find an existing publish job
                job_instance = JobInstance.objects.filter(
                    pipeline_instance=pipeline, execution_parameters__job_name="publish"
                ).first()

                if job_instance:
                    # Use the existing job, but update its status
                    job_instance.status = JobInstance.Status.PENDING
                    job_instance.save()
                else:
                    # Create a new job (with a unique job_template)
                    existing_job_instance = JobInstance.objects.filter(
                        pipeline_instance=pipeline
                    ).first()

                    if (
                        existing_job_instance is None
                        or existing_job_instance.job_template is None
                    ):
                        logger.error(
                            f"No existing job instances or templates found for pipeline {pipeline.id}"
                        )
                        return

                    job_template = existing_job_instance.job_template

                    # Create the job
                    job_instance = JobInstance.objects.create(
                        pipeline_instance=pipeline,
                        job_template=job_template,
                        status=JobInstance.Status.PENDING,
                        execution_parameters={
                            "job_name": "publish",
                            "workflow": publish_job_def.workflow,
                            "ref": publish_job_def.ref,
                            "inputs": publish_job_def.inputs,
                        },
                    )
            except Exception as e:
                logger.exception(f"Failed to create job instance: {str(e)}")
                return

            # Trigger the GitHub Action
            provider_impl = GitHubActionsProvider(provider)

            try:
                # Use sync dispatch since we're running in a command
                import asyncio

                external_job_id = asyncio.run(provider_impl.dispatch_job(job_instance))

                job_instance.external_job_id = external_job_id
                job_instance.status = JobInstance.Status.RUNNING
                job_instance.started_at = timezone.now()
                job_instance.save()

                self.stdout.write(
                    f"  - Dispatched publish job for pipeline #{pipeline.id}"
                )
            except Exception as e:
                logger.exception(f"Failed to dispatch publish job: {str(e)}")
                job_instance.status = JobInstance.Status.FAILED
                job_instance.results = {"error": str(e)}
                job_instance.finished_at = timezone.now()
                job_instance.save()
                self.stdout.write(
                    self.style.ERROR(f"  - Failed to dispatch publish job: {str(e)}")
                )

    def _publish_db_pipeline(self, pipeline):
        """Publish a database-defined pipeline"""
        from pipelines.models import JobTemplate

        # Find publish job template
        publish_job_template = JobTemplate.objects.filter(
            pipeline_template=pipeline.pipeline_template, name__icontains="publish"
        ).first()

        if not publish_job_template:
            logger.error(f"No publish job template found for pipeline #{pipeline.id}")
            return

        provider = publish_job_template.provider

        # Create and run the publish job
        with transaction.atomic():
            # Update pipeline status
            pipeline.status = PipelineInstance.Status.PUBLISHED
            pipeline.save()

            # Check if there's already a job instance for this job template
            job_instance = JobInstance.objects.filter(
                pipeline_instance=pipeline, job_template=publish_job_template
            ).first()

            if job_instance:
                # Use the existing job but update its status
                job_instance.status = JobInstance.Status.PENDING
                job_instance.save()
            else:
                # Create a new job instance
                job_instance = JobInstance.objects.create(
                    pipeline_instance=pipeline,
                    job_template=publish_job_template,
                    status=JobInstance.Status.PENDING,
                )

            # Trigger the GitHub Action
            provider_impl = GitHubActionsProvider(provider)

            try:
                # Use sync dispatch since we're running in a command
                import asyncio

                external_job_id = asyncio.run(provider_impl.dispatch_job(job_instance))

                job_instance.external_job_id = external_job_id
                job_instance.status = JobInstance.Status.RUNNING
                job_instance.started_at = timezone.now()
                job_instance.save()

                self.stdout.write(
                    f"  - Dispatched publish job for pipeline #{pipeline.id}"
                )
            except Exception as e:
                logger.exception(f"Failed to dispatch publish job: {str(e)}")
                job_instance.status = JobInstance.Status.FAILED
                job_instance.results = {"error": str(e)}
                job_instance.finished_at = timezone.now()
                job_instance.save()
                self.stdout.write(
                    self.style.ERROR(f"  - Failed to dispatch publish job: {str(e)}")
                )
