import json
import re
import zipfile
from io import BytesIO
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

    def extract_run_id_from_log_url(self, log_url: str) -> int | None:
        """Extract run_id from GitHub Actions log URL."""
        if not log_url:
            return None

        pattern = r"github\.com/[^/]+/[^/]+/actions/runs/(\d+)"
        match = re.search(pattern, log_url)

        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None

        return None

    async def fetch_run_logs(self, owner: str, repo: str, run_id: int) -> str | None:
        """Fetch and extract log content from a GitHub Actions run."""
        try:
            async with self._get_client() as client:
                response = await client.get(
                    f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
                )
                response.raise_for_status()

                zip_content = BytesIO(response.content)
                log_content_parts = []

                with zipfile.ZipFile(zip_content) as zip_file:
                    for file_info in zip_file.filelist:
                        if file_info.filename.endswith(".txt"):
                            with zip_file.open(file_info) as log_file:
                                content = log_file.read().decode(
                                    "utf-8", errors="ignore"
                                )
                                lines = content.split("\n")
                                if len(lines) > 1000:
                                    content = "\n".join(lines[-1000:])
                                log_content_parts.append(content)

                return "\n".join(log_content_parts)

        except Exception:
            return None

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
