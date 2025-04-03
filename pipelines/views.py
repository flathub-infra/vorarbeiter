import json
import logging
import asyncio
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.shortcuts import get_object_or_404

from .models import JobInstance
from .services import PipelineRunner
from .build_pipeline import BuildPipelineRunner

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def job_callback(request, callback_id):
    try:
        data = json.loads(request.body)
        status = data.get("status")

        if not status:
            return JsonResponse(
                {"status": "error", "message": "Missing status"}, status=400
            )

        job = get_object_or_404(JobInstance, callback_id=callback_id)

        is_build_pipeline = bool(
            job.pipeline_instance.trigger_parameters.get("pipeline_name")
        )

        if is_build_pipeline:
            if status == "success":
                job_status = JobInstance.Status.SUCCEEDED
            elif status == "failure":
                job_status = JobInstance.Status.FAILED
            elif status == "cancelled":
                job_status = JobInstance.Status.CANCELLED
            else:
                return JsonResponse(
                    {"status": "error", "message": f"Invalid status: {status}"},
                    status=400,
                )

            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                loop.run_until_complete(
                    BuildPipelineRunner.handle_job_callback(
                        job, job_status, data.get("results", {})
                    )
                )
                loop.close()
            except Exception as e:
                logger.exception(
                    f"Error processing build pipeline job callback: {str(e)}"
                )
                return JsonResponse({"status": "error", "message": str(e)}, status=500)

        else:
            if status == "success":
                job.status = JobInstance.Status.SUCCEEDED
            elif status == "failure":
                job.status = JobInstance.Status.FAILED
            elif status == "cancelled":
                job.status = JobInstance.Status.CANCELLED
            else:
                return JsonResponse(
                    {"status": "error", "message": f"Invalid status: {status}"},
                    status=400,
                )

            job.finished_at = timezone.now()

            if data.get("results"):
                job.results = data.get("results")

            job.save()

            PipelineRunner._check_pipeline_status(job.pipeline_instance)

        return JsonResponse({"status": "success"})

    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        logger.exception(f"Error processing job callback: {str(e)}")
        return JsonResponse({"status": "error", "message": str(e)}, status=500)
