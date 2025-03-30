import aiohttp
from typing import Dict, Any
import logging

from .base import BaseProvider

logger = logging.getLogger(__name__)


class GitHubActionsProvider(BaseProvider):
    async def dispatch_job(self, job_instance):
        token = self.provider_model.credentials.get("token")
        owner = self.provider_model.settings.get("owner")
        repo = self.provider_model.settings.get("repo")

        if not all([token, owner, repo]):
            raise ValueError(
                "Missing required credentials or settings: token, owner, repo"
            )

        workflow = job_instance.job_template.provider_config.get("workflow")
        inputs = job_instance.job_template.provider_config.get("inputs", {})

        if not workflow:
            raise ValueError("Missing required provider_config: workflow")

        processed_inputs = self._process_inputs(
            inputs, job_instance.execution_parameters
        )

        url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        }

        ref = job_instance.job_template.provider_config.get("ref", "main")

        callback_url = f"{self.provider_model.settings.get('callback_base_url', '')}/api/jobs/{job_instance.callback_id}/callback"

        payload = {
            "ref": ref,
            "inputs": {**processed_inputs, "callback_url": callback_url},
        }

        logger.info(f"Dispatching GitHub workflow: {url}, payload: {payload}")

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status != 204:
                    error_text = await response.text()
                    logger.error(f"Failed to dispatch workflow: {error_text}")
                    raise Exception(f"Failed to dispatch workflow: {error_text}")

                run_id = await self._find_workflow_run_by_callback(
                    session,
                    owner,
                    repo,
                    workflow,
                    headers,
                    str(job_instance.callback_id),
                )

                logger.info(f"Workflow dispatched successfully, run ID: {run_id}")

                job_instance.logs_url = (
                    f"https://github.com/{owner}/{repo}/actions/runs/{run_id}"
                )
                job_instance.save(update_fields=["logs_url"])

                return str(run_id)

    async def _find_workflow_run_by_callback(
        self, session, owner, repo, workflow, headers, callback_id
    ):
        url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow}/runs"
        params = {"per_page": 5}

        async with session.get(url, headers=headers, params=params) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"Failed to get workflow runs: {error_text}")

            data = await response.json()
            if not data.get("workflow_runs"):
                raise Exception("No workflow runs found")

            run = data["workflow_runs"][0]
            return run["id"]

    async def cancel_job(self, job_instance):
        token = self.provider_model.credentials.get("token")
        owner = self.provider_model.settings.get("owner")
        repo = self.provider_model.settings.get("repo")

        run_id = job_instance.external_job_id

        if not run_id:
            logger.warning(f"Cannot cancel job {job_instance.id}: no external_job_id")
            return False

        url = (
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/cancel"
        )
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

        logger.info(f"Cancelling GitHub workflow run: {run_id}")

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers) as response:
                success = response.status == 202

                if not success:
                    error_text = await response.text()
                    logger.error(f"Failed to cancel workflow run: {error_text}")
                else:
                    logger.info(
                        f"Workflow run {run_id} cancellation requested successfully"
                    )

                return success

    def _process_inputs(
        self, inputs: Dict[str, Any], execution_parameters: Dict[str, Any]
    ) -> Dict[str, str]:
        processed_inputs = {}

        for key, value in inputs.items():
            if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
                param_name = value.strip("{}")
                if param_name.startswith("execution_parameters."):
                    nested_param = param_name.split(".", 1)[1]
                    if nested_param in execution_parameters:
                        processed_inputs[key] = str(execution_parameters[nested_param])
                    else:
                        processed_inputs[key] = value
                else:
                    processed_inputs[key] = value
            else:
                processed_inputs[key] = str(value)

        return processed_inputs
