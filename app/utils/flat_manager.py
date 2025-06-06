from enum import IntEnum
from typing import Any, NotRequired, TypedDict
from urllib.parse import urlparse

import httpx
import structlog

logger = structlog.get_logger(__name__)


class JobStatus(IntEnum):
    NEW = 0
    STARTED = 1
    ENDED = 2
    BROKEN = 3


class JobKind(IntEnum):
    COMMIT = 0
    PUBLISH = 1
    UPDATE_REPO = 2
    REPUBLISH = 3
    CHECK = 4
    PRUNE = 5


class BuildResponse(TypedDict):
    id: int
    status: str
    repo: str
    ref: NotRequired[str]


class TokenResponse(TypedDict):
    token: str
    sub: str
    scope: list[str]


class JobResponse(TypedDict):
    id: int
    kind: JobKind
    status: JobStatus
    repo: str
    contents: str
    results: str
    log: str


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

    async def create_token_subset(self, build_id: int, app_id: str) -> str:
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

    def get_build_url(self, build_id: int | str) -> str:
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

    def get_flatpakref_url(self, build_id: int, app_id: str) -> str:
        return f"https://dl.flathub.org/build-repo/{build_id}/{app_id}.flatpakref"

    async def commit(
        self,
        build_id: int,
        end_of_life: str | None = None,
        end_of_life_rebase: str | None = None,
    ):
        build_url = self.get_build_url(build_id)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{build_url}/commit",
                headers=self.headers,
                json={
                    "endoflife": end_of_life,
                    "endoflife_rebase": end_of_life_rebase,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()

    async def publish(self, build_id: int):
        build_url = self.get_build_url(build_id)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{build_url}/publish",
                headers=self.headers,
                json={},
                timeout=self.timeout,
            )
            response.raise_for_status()

    async def purge(self, build_id: int):
        build_url = self.get_build_url(build_id)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{build_url}/purge",
                headers=self.headers,
                json={},
                timeout=self.timeout,
            )
            response.raise_for_status()

    async def get_build_info(self, build_id: int) -> dict[str, Any]:
        build_url = self.get_build_url(build_id)
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{build_url}/extended",
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()

    async def get_job(self, job_id: int) -> JobResponse:
        async with httpx.AsyncClient() as client:
            response = await client.request(
                "GET",
                f"{self.url}/api/v1/job/{job_id}",
                headers=self.headers,
                json={"log_offset": None},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
