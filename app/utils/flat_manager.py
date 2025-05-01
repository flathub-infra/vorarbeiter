from typing import Any, NotRequired, TypedDict
from urllib.parse import urlparse

import httpx
import structlog

logger = structlog.get_logger(__name__)


class BuildResponse(TypedDict):
    id: str
    status: str
    repo: str
    ref: NotRequired[str]


class TokenResponse(TypedDict):
    token: str
    sub: str
    scope: list[str]


class FlatManagerClient:
    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self.url = url
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout = timeout

    async def create_build(self, repo: str, build_log_url: str) -> BuildResponse:
        logger.debug("Creating build in flat-manager", repo=repo)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.url}/api/v1/build",
                    json={
                        "repo": repo,
                        "build-log-url": build_log_url,
                    },
                    headers=self.headers,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data: BuildResponse = response.json()
                logger.info(
                    "Successfully created build in flat-manager",
                    build_id=data["id"],
                    repo=repo,
                )
                return data
            except httpx.HTTPStatusError as e:
                logger.error(
                    "Failed to create build in flat-manager",
                    repo=repo,
                    status_code=e.response.status_code,
                    response_text=e.response.text,
                )
                raise
            except Exception as e:
                logger.error(
                    "Unexpected error creating build in flat-manager",
                    repo=repo,
                    error=str(e),
                )
                raise

    async def create_token_subset(self, build_id: str, app_id: str) -> str:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.url}/api/v1/token_subset",
                json={
                    "name": "upload",
                    "sub": f"build/{build_id}",
                    "scope": ["upload"],
                    "prefix": [app_id],
                    "duration": 24 * 60 * 60,
                },
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data: TokenResponse = response.json()
            return data["token"]

    def get_build_url(self, build_id: str) -> str:
        # Handle case where build_id is already a full URL
        if isinstance(build_id, str) and (
            build_id.startswith("http://") or build_id.startswith("https://")
        ):
            path_parts = urlparse(build_id).path.rstrip("/").split("/")
            if path_parts:
                numeric_id = path_parts[-1]
                return f"{self.url}/api/v1/build/{numeric_id}"
            return build_id

        return f"{self.url}/api/v1/build/{build_id}"

    def get_flatpakref_url(self, build_id: str, app_id: str) -> str:
        return f"https://dl.flathub.org/build-repo/{build_id}/{app_id}.flatpakref"

    async def commit(self, build_id: str):
        build_url = self.get_build_url(build_id)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{build_url}/commit",
                headers=self.headers,
                json={
                    "endoflife": None,
                    "endoflife_rebase": None,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()

    async def publish(self, build_id: str):
        build_url = self.get_build_url(build_id)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{build_url}/publish",
                headers=self.headers,
                json={},
                timeout=self.timeout,
            )
            response.raise_for_status()

    async def purge(self, build_id: str):
        build_url = self.get_build_url(build_id)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{build_url}/purge",
                headers=self.headers,
                json={},
                timeout=self.timeout,
            )
            response.raise_for_status()

    async def get_build_info(self, build_id: str) -> dict[str, Any]:
        build_url = self.get_build_url(build_id)
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{build_url}/extended",
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
