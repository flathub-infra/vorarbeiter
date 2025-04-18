import json
from typing import Any, Dict

import httpx

from app.config import settings
from app.providers.base import JobProvider, ProviderType


class GnomeGitlabJobProvider(JobProvider):
    provider_type = ProviderType.GNOME_GITLAB

    def __init__(self) -> None:
        self.token = settings.gnome_gitlab_token
        self.client: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        if not self.token:
            raise ValueError("GNOME_GITLAB_TOKEN is not set.")

        self.client = httpx.AsyncClient(
            base_url="https://gitlab.gnome.org/api/v4",
            headers={
                "PRIVATE-TOKEN": self.token,
                "Content-Type": "application/json",
            },
        )

    async def dispatch(
        self, job_id: str, pipeline_id: str, job_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Dispatches a GitLab pipeline."""
        if self.client is None:
            await self.initialize()
            if self.client is None:
                raise RuntimeError("Client failed to initialize.")

        project_id = 34050

        params = job_data.get("params", {})
        ref = params.get("ref", "main")
        variables = params.get("variables", {})

        pipeline_variables = {
            **variables,
            "VORARBEITER_JOB_ID": job_id,
            "VORARBEITER_PIPELINE_ID": pipeline_id,
            "APP_ID": job_data.get("app_id", ""),
        }

        payload = {
            "ref": ref,
            "variables": [
                {"key": k, "value": str(v)} for k, v in pipeline_variables.items()
            ],
        }

        try:
            response = await self.client.post(
                f"/projects/{project_id}/pipeline",
                content=json.dumps(payload),
            )
            response.raise_for_status()

            response_data = response.json()

            if response.status_code == 201:
                gitlab_pipeline_id = response_data.get("id")
                web_url = response_data.get("web_url")
                status = response_data.get("status", "created")

                return {
                    "status": status,
                    "job_id": job_id,
                    "pipeline_id": pipeline_id,
                    "provider_job_id": gitlab_pipeline_id,
                    "project_id": project_id,
                    "ref": ref,
                    "web_url": web_url,
                }
            else:
                raise httpx.HTTPStatusError(
                    f"Unexpected status code {response.status_code}",
                    request=response.request,
                    response=response,
                )

        except httpx.HTTPStatusError as e:
            print(f"Error dispatching GitLab pipeline for project {project_id}: {e}")
            print(f"Response content: {e.response.text}")
            raise e
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            raise e

    async def cancel(self, job_id: str, provider_data: Dict[str, Any]) -> bool:
        """Cancels a GitLab pipeline."""
        if self.client is None:
            await self.initialize()
            if self.client is None:
                raise RuntimeError("Client failed to initialize.")

        project_id = provider_data.get("project_id")
        gitlab_pipeline_id = provider_data.get("provider_job_id")

        if not project_id or not gitlab_pipeline_id:
            print(
                f"Cannot cancel job {job_id}: Missing project_id ('{project_id}') or provider_job_id ('{gitlab_pipeline_id}') in provider_data"
            )
            return False

        try:
            print(
                f"Attempting to cancel GitLab pipeline {gitlab_pipeline_id} for project {project_id} (Job ID: {job_id})"
            )
            response = await self.client.post(
                f"/projects/{project_id}/pipelines/{gitlab_pipeline_id}/cancel"
            )

            if response.status_code == 200:
                print(
                    f"Successfully requested cancellation for GitLab pipeline {gitlab_pipeline_id}"
                )
                return True
            else:
                print(
                    f"Failed to cancel GitLab pipeline {gitlab_pipeline_id}. Status: {response.status_code}, Response: {response.text}"
                )
                return False

        except httpx.HTTPStatusError as e:
            print(
                f"Error cancelling GitLab pipeline {gitlab_pipeline_id} for project {project_id}: {e}"
            )
            print(f"Response content: {e.response.text}")
            return False
        except Exception as e:
            print(f"An unexpected error occurred during cancellation: {e}")
            return False
