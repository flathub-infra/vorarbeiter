from django.core.management.base import BaseCommand
import asyncio
from asgiref.sync import sync_to_async
from django.db import transaction
from pipelines.build_pipeline import BuildPipelineRunner
from pipelines.models import JobInstance, Provider, PipelineInstance


class Command(BaseCommand):
    help = "Creates and runs a test build pipeline"

    def add_arguments(self, parser):
        parser.add_argument("--app-id", default="org.test.App", help="Application ID")
        parser.add_argument("--token", default="test-token", help="Token for GitHub")
        parser.add_argument("--branch", default="main", help="Branch to build")
        parser.add_argument("--build-id", default="test-build-123", help="Build ID")
        parser.add_argument("--provider-id", type=int, help="Provider ID to use")

    def handle(self, *args, **options):
        self.stdout.write("Creating test build pipeline...")

        trigger_params = {
            "app_id": options["app_id"],
            "token": options["token"],
            "branch": options["branch"],
            "build_id": options["build_id"],
        }

        provider_id = options["provider_id"]
        if not provider_id:
            provider = Provider.objects.filter(provider_type="github_actions").first()
            if provider:
                provider_id = provider.id
                self.stdout.write(
                    f"Using provider: {provider.name} (ID: {provider_id})"
                )
            else:
                self.stdout.write(
                    self.style.WARNING("No GitHub Actions provider found!")
                )

        @sync_to_async
        def create_pipeline():
            with transaction.atomic():
                return BuildPipelineRunner.create_and_run_pipeline(
                    "build", trigger_params, github_provider_id=provider_id
                )

        @sync_to_async
        def get_job_details(pipeline_id):
            pipeline_obj = PipelineInstance.objects.get(id=pipeline_id)
            jobs = JobInstance.objects.filter(pipeline_instance=pipeline_obj)
            return pipeline_obj, [
                (job, job.execution_parameters.get("job_name", "unknown"))
                for job in jobs
            ]

        try:
            pipeline_coroutine = asyncio.run(create_pipeline())
            pipeline = asyncio.run(pipeline_coroutine)
            pipeline_obj, jobs_with_names = asyncio.run(get_job_details(pipeline.id))
            self.stdout.write(self.style.SUCCESS(f"Created pipeline: {pipeline.id}"))

            self.stdout.write("Jobs:")
            for job, job_name in jobs_with_names:
                self.stdout.write(f"- {job_name}: Status={job.status}")
                if job.logs_url:
                    self.stdout.write(f"  Logs: {job.logs_url}")

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error running pipeline: {str(e)}"))
