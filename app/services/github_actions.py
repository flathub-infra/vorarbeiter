import json
from typing import Any

import httpx

from app.config import settings


class GitHubActionsService:
    base_url = "https://api.github.com"

    def __init__(self) -> None:
        self.token = settings.github_token

    def _get_client(self) -> httpx.AsyncClient:
        """Create a properly configured HTTP client for GitHub API access."""
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
        )

    async def dispatch(
        self, job_id: str, pipeline_id: str, job_data: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._get_client() as client:
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

            response = await client.post(
                f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
                content=json.dumps(payload),
            )

            response.raise_for_status()

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

        async with self._get_client() as client:
            response = await client.post(
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel"
            )

            return response.status_code == 202
