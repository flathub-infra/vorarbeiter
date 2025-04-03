from django.core.management.base import BaseCommand
import asyncio
import threading
import queue
from typing import Tuple, Any
from django.db import transaction
from pipelines.build_pipeline import BuildPipelineRunner
from pipelines.models import JobInstance, Provider


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

        def run_pipeline() -> Tuple[str, Any]:
            result_queue: queue.Queue[Tuple[str, Any]] = queue.Queue()

            def thread_task():
                try:
                    with transaction.atomic():
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            pipeline = loop.run_until_complete(
                                BuildPipelineRunner.create_and_run_pipeline(
                                    "build",
                                    trigger_params,
                                    github_provider_id=provider_id,
                                )
                            )
                            result_queue.put(("success", pipeline))
                        except Exception as e:
                            result_queue.put(("error", str(e)))
                        finally:
                            loop.close()
                except Exception as e:
                    result_queue.put(("error", str(e)))

            thread = threading.Thread(target=thread_task)
            thread.start()
            thread.join()

            if not result_queue.empty():
                return result_queue.get()
            return ("error", "No result returned")

        status, result = run_pipeline()

        if status == "error":
            self.stdout.write(self.style.ERROR(f"Error running pipeline: {result}"))
            return

        pipeline = result
        self.stdout.write(self.style.SUCCESS(f"Created pipeline: {pipeline.id}"))

        jobs = JobInstance.objects.filter(pipeline_instance=pipeline)

        self.stdout.write("Jobs:")
        for job in jobs:
            job_name = job.execution_parameters.get("job_name", "unknown")
            self.stdout.write(f"- {job_name}: Status={job.status}")
            if job.logs_url:
                self.stdout.write(f"  Logs: {job.logs_url}")
