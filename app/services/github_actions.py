import json
import zipfile
import structlog
from io import BytesIO
from typing import Any

import httpx

from app.utils.github import get_check_run_annotations, get_github_actions_client

logger = structlog.get_logger(__name__)


class GitHubActionsService:
    base_url = "https://api.github.com"

    async def dispatch(
        self, job_id: str, pipeline_id: str, job_data: dict[str, Any]
    ) -> dict[str, Any]:
        params = job_data.get("params", {})
        owner = params["owner"]
        repo = params["repo"]
        workflow_id = params["workflow_id"]
        ref = params.get("ref", "main")
        app_id = job_data.get("app_id", "")

        inputs = params.get("inputs", {})
        workflow_inputs = {
            **inputs,
            "app_id": app_id,
        }

        additional_params = params.get("additional_params", {})
        if additional_params:
            workflow_inputs.update(additional_params)

        payload = {
            "ref": ref,
            "inputs": workflow_inputs,
        }

        client = get_github_actions_client()
        url = f"{self.base_url}/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches"
        response = await client.request(
            "post",
            url,
            content=json.dumps(payload),
            context={"owner": owner, "repo": repo, "workflow_id": workflow_id},
        )

        if not response:
            raise httpx.HTTPStatusError(
                "Failed to dispatch workflow",
                request=httpx.Request("POST", url),
                response=httpx.Response(500),
            )

        return {
            "status": "dispatched",
            "job_id": job_id,
            "pipeline_id": pipeline_id,
            "owner": owner,
            "repo": repo,
            "workflow_id": workflow_id,
            "ref": ref,
        }

    async def cancel(self, job_id: str, provider_data: dict[str, Any]) -> bool:
        run_id = provider_data.get("run_id")
        if not run_id:
            return False

        owner = provider_data.get("owner")
        repo = provider_data.get("repo")

        if not owner or not repo:
            return False

        client = get_github_actions_client()
        url = f"{self.base_url}/repos/{owner}/{repo}/actions/runs/{run_id}/cancel"
        response = await client.request(
            "post",
            url,
            context={"owner": owner, "repo": repo, "run_id": run_id},
        )

        return response is not None and response.status_code == 202

    async def get_workflow_run_details(
        self, owner: str, repo: str, run_id: int
    ) -> dict | None:
        client = get_github_actions_client()
        url = f"{self.base_url}/repos/{owner}/{repo}/actions/runs/{run_id}"
        response = await client.request(
            "get",
            url,
            context={"owner": owner, "repo": repo, "run_id": run_id},
        )

        if response:
            return response.json()
        return None

    async def download_run_logs(self, owner: str, repo: str, run_id: int) -> str | None:
        client = get_github_actions_client()
        url = f"{self.base_url}/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
        response = await client.request(
            "get",
            url,
            context={"owner": owner, "repo": repo, "run_id": run_id},
        )

        if not response:
            return None

        try:
            zip_content = BytesIO(response.content)
            log_content = ""

            with zipfile.ZipFile(zip_content, "r") as zip_file:
                for file_info in zip_file.filelist:
                    if file_info.filename.endswith(".txt"):
                        with zip_file.open(file_info) as log_file:
                            log_content += log_file.read().decode(
                                "utf-8", errors="ignore"
                            )

            return log_content
        except Exception as e:
            logger.error("Error extracting logs", error=str(e))
            return None

    async def check_run_was_cancelled(self, provider_data: dict[str, Any]) -> bool:
        run_id = provider_data.get("run_id")
        owner = provider_data.get("owner")
        repo = provider_data.get("repo")

        if not all([run_id, owner, repo]):
            logger.warning(
                "Missing required provider_data fields for cancellation check",
                has_run_id=bool(run_id),
                has_owner=bool(owner),
                has_repo=bool(repo),
            )
            return False

        if not isinstance(owner, str) or not isinstance(repo, str):
            logger.warning("owner and repo must be strings")
            return False

        if run_id is None:
            logger.warning("run_id cannot be None")
            return False

        try:
            run_id_int = int(run_id)
        except (ValueError, TypeError):
            logger.warning("Invalid run_id format", run_id=run_id)
            return False

        run_details = await self.get_workflow_run_details(owner, repo, run_id_int)
        if run_details:
            run_attempt = run_details.get("run_attempt", 1)
            if run_attempt > 1:
                logger.info(
                    "Run attempt - treating as failure since this is a retry",
                    run_id=run_id_int,
                    run_attempt=run_attempt,
                )
                return False

        annotations = await get_check_run_annotations(owner, repo, run_id_int)
        if annotations is None:
            logger.warning("Failed to fetch job annotations for cancellation detection")
            return False

        cancel_annotation_strs = (
            "The operation was canceled.",
            "The job was not acquired by Runner of",
        )

        cancelled = any(
            any((a.get("message", "")).startswith(s) for s in cancel_annotation_strs)
            for a in annotations
        )

        has_exit_or_user_cancel = any(
            a.get("message", "").startswith(
                (
                    "Process completed with exit code",
                    "The run was canceled by",
                    "Canceling since a higher priority waiting request for",
                )
            )
            for a in annotations
        )

        if cancelled and not has_exit_or_user_cancel:
            logger.info(
                "Run detected as cancelled by GitHub via job annotation",
                run_id=run_id_int,
            )
            return True

        return False
