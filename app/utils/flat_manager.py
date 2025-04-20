from typing import TypedDict, NotRequired

import httpx


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
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"}

    async def create_build(self, repo: str, build_log_url: str) -> BuildResponse:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.url}/api/v1/build",
                json={
                    "repo": repo,
                    "build-log-url": build_log_url,
                },
                headers=self.headers,
                timeout=30.0,
            )
            response.raise_for_status()
            data: BuildResponse = response.json()
            return data

    async def create_token_subset(self, build_id: str, app_id: str) -> str:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.url}/api/v1/token_subset",
                json={
                    "name": "upload",
                    "sub": f"build/{build_id}",
                    "scope": ["upload"],
                    "prefix": [app_id],
                    "duration": 6 * 60 * 60,
                },
                headers=self.headers,
                timeout=30.0,
            )
            response.raise_for_status()
            data: TokenResponse = response.json()
            return data["token"]

    def get_build_url(self, build_id: str) -> str:
        return f"{self.url}/api/v1/build/{build_id}"
