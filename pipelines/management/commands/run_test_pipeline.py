from django.core.management.base import BaseCommand
import asyncio
import threading
import queue
import traceback
from typing import Tuple, Any
from django.db import transaction
from pipelines.models import (
    JobInstance,
    Provider,
    PipelineInstance,
    PipelineTemplate,
    JobTemplate,
)


class Command(BaseCommand):
    help = "Creates and runs a test build pipeline"

    def add_arguments(self, parser):
        parser.add_argument("--app-id", default="org.test.App", help="Application ID")
        parser.add_argument("--token", default="test-token", help="Token for GitHub")
        parser.add_argument("--branch", default="main", help="Branch to build")
        parser.add_argument("--build-id", default="test-build-123", help="Build ID")
        parser.add_argument("--provider-id", type=int, help="Provider ID to use")
        parser.add_argument(
            "--debug",
            action="store_true",
            help="Enable debug mode with detailed traceback",
        )
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Actually dispatch jobs to the provider",
        )

    def handle(self, *args, **options):
        self.stdout.write("Creating test build pipeline...")
        self.debug_mode = options.get("debug", False)
        self.execute_mode = options.get("execute", False)

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
                return

        trigger_params = {
            "app_id": options["app_id"],
            "token": options["token"],
            "branch": options["branch"],
            "build_id": options["build_id"],
        }

        try:
            template, created = PipelineTemplate.objects.get_or_create(
                name="test-pipeline",
                version=1,
                defaults={
                    "description": "Test pipeline created by run_test_pipeline command"
                },
            )

            pipeline_instance = PipelineInstance.objects.create(
                pipeline_template=template,
                status=PipelineInstance.Status.RUNNING,
                trigger_parameters={"pipeline_name": "build", **trigger_params},
            )

            status, result = self._run_in_thread(
                trigger_params, provider_id, pipeline_instance.id, self.execute_mode
            )

            if status == "error":
                self.stdout.write(self.style.ERROR(f"Error running pipeline: {result}"))
                return

            self.stdout.write(
                self.style.SUCCESS(f"Created pipeline: {pipeline_instance.id}")
            )

            jobs = JobInstance.objects.filter(pipeline_instance=pipeline_instance)

            self.stdout.write("Jobs:")
            for job in jobs:
                job_name = job.execution_parameters.get("job_name", "unknown")
                self.stdout.write(f"- {job_name}: Status={job.status}")
                if job.logs_url:
                    self.stdout.write(f"  Logs: {job.logs_url}")

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error: {str(e)}"))
            if self.debug_mode:
                self.stdout.write(self.style.ERROR(traceback.format_exc()))

    def _run_in_thread(
        self, trigger_params, provider_id, pipeline_id, execute_mode=False
    ) -> Tuple[str, Any]:
        debug_mode = self.debug_mode
        result_queue: queue.Queue[Tuple[str, Any]] = queue.Queue()

        def thread_task():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                try:
                    pipeline_definition = {
                        "validate_manifest": {
                            "name": "Validate manifest",
                            "workflow": "validate-manifest.yml",
                            "ref": "main",
                            "inputs": {"app_id": "{app_id}"},
                            "depends_on": [],
                        },
                        "prepare_build": {
                            "name": "Prepare Build",
                            "workflow": "prepare-build.yml",
                            "ref": "main",
                            "inputs": {"token": "{token}"},
                            "depends_on": ["validate_manifest"],
                        },
                        "build_x86_64": {
                            "name": "Build x86_64",
                            "workflow": "build.yml",
                            "ref": "main",
                            "inputs": {"branch": "{branch}", "arch": "x86_64"},
                            "depends_on": ["prepare_build"],
                        },
                        "build_aarch64": {
                            "name": "Build aarch64",
                            "workflow": "build.yml",
                            "ref": "main",
                            "inputs": {"branch": "{branch}", "arch": "aarch64"},
                            "depends_on": ["prepare_build"],
                        },
                        "publish": {
                            "name": "Publish",
                            "workflow": "publish.yml",
                            "ref": "main",
                            "inputs": {"build_id": "{build_id}", "token": "{token}"},
                            "depends_on": ["build_x86_64", "build_aarch64"],
                        },
                    }

                    with transaction.atomic():
                        pipeline_instance = PipelineInstance.objects.get(id=pipeline_id)

                    import uuid as uuid_module

                    for job_name, job_def in pipeline_definition.items():
                        job_template, created = JobTemplate.objects.get_or_create(
                            pipeline_template=pipeline_instance.pipeline_template,
                            name=job_name,
                            defaults={
                                "provider_id": provider_id,
                                "provider_config": {
                                    "workflow": job_def["workflow"],
                                    "ref": job_def["ref"],
                                    "job_name": job_name,
                                },
                                "description": f"Auto-created job template for {job_name}",
                            },
                        )

                        JobInstance.objects.create(
                            pipeline_instance=pipeline_instance,
                            job_template=job_template,
                            status=JobInstance.Status.PENDING,
                            callback_id=uuid_module.uuid4(),
                            execution_parameters={
                                "job_name": job_name,
                                "workflow": job_def["workflow"],
                                "ref": job_def["ref"],
                                "inputs": job_def["inputs"],
                                **trigger_params,
                            },
                        )

                    root_jobs = [
                        job_name
                        for job_name, job_def in pipeline_definition.items()
                        if not job_def["depends_on"]
                    ]

                    from django.utils import timezone

                    for job_name in root_jobs:
                        job_inst = JobInstance.objects.get(
                            pipeline_instance=pipeline_instance,
                            job_template__name=job_name,
                        )
                        job_inst.status = JobInstance.Status.QUEUED
                        job_inst.queued_at = timezone.now()
                        job_inst.save()

                        if execute_mode:
                            try:
                                provider = Provider.objects.get(id=provider_id)
                                provider.get_implementation()

                                job_inst.status = JobInstance.Status.RUNNING
                                job_inst.started_at = timezone.now()
                                job_inst.save()

                                print(f"Dispatching job {job_name} to provider...")

                                provider_impl = provider.get_implementation()
                                asyncio.run(provider_impl.dispatch_job(job_inst))
                            except Exception as e:
                                error_message = (
                                    f"Failed to dispatch job {job_name}: {str(e)}"
                                )
                                print(error_message)
                                if debug_mode:
                                    print(traceback.format_exc())

                                job_inst.status = JobInstance.Status.FAILED
                                job_inst.results = {"error": error_message}
                                job_inst.finished_at = timezone.now()
                                job_inst.save()
                                raise

                    if execute_mode:
                        result_queue.put(
                            (
                                "success",
                                "Pipeline jobs created and dispatched to provider",
                            )
                        )
                    else:
                        result_queue.put(
                            (
                                "success",
                                "Pipeline jobs created and queued (use --execute to dispatch)",
                            )
                        )

                except Exception as e:
                    result_queue.put(("error", str(e)))
                finally:
                    loop.close()
            except Exception as e:
                result_queue.put(("error", str(e)))

        thread = threading.Thread(target=thread_task)
        thread.daemon = True
        thread.start()
        thread.join()

        if result_queue.empty():
            return ("error", "No result returned")

        return result_queue.get()
