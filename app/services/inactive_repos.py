import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InactiveRepoSnapshot
from app.utils.github import GitHubAPIResult
from app.utils.github_app import GitHubAppInstallationAuth

logger = structlog.get_logger(__name__)

BOT_AUTHORS = {"dependabot[bot]", "flathubbot", "github-actions[bot]"}
PAGE_SIZE = 100
PR_THRESHOLD = 5
SERVER_RETRIES = 3


class InactiveRepoScanError(RuntimeError):
    pass


class GitHubLegalRestrictionError(InactiveRepoScanError):
    pass


@dataclass(frozen=True)
class InactiveRepoScanResult:
    organization: str
    scan_started_at: datetime
    scan_completed_at: datetime
    automatic_candidates: list[str]
    repositories_seen: int
    repositories_checked: int
    repositories_at_pr_threshold: int
    unobservable_repositories: list[str] = field(default_factory=list)


class ScannerClient(Protocol):
    async def request_with_result(
        self, method: str, url: str, **kwargs: Any
    ) -> GitHubAPIResult: ...


class ScannerAuth(Protocol):
    async def get_client(self) -> ScannerClient: ...

    def invalidate(self) -> None: ...


class InactiveRepoScanner:
    def __init__(
        self,
        auth: ScannerAuth | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.auth = auth or GitHubAppInstallationAuth()
        self.sleep = sleep

    def _rate_limit_delay(self, result: GitHubAPIResult) -> float:
        if result.retry_after is not None:
            return result.retry_after
        response = result.response
        if response is None:
            return 60.0
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 0)
            except ValueError:
                pass
        reset = response.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return max(float(reset) - time.time(), 0) + 1
            except ValueError:
                pass
        return 60.0

    def _is_rate_limited(self, result: GitHubAPIResult) -> bool:
        if result.error_type == "rate_limit":
            return True
        response = result.response
        if response is None:
            return False
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return True
        try:
            body = response.json()
        except ValueError:
            return False
        return (
            isinstance(body, dict)
            and "rate limit" in str(body.get("message", "")).lower()
        )

    async def _request_json(
        self, url: str, *, params: dict[str, Any], context: dict[str, Any]
    ) -> Any:
        authentication_retried = False
        server_attempt = 0
        while True:
            client = await self.auth.get_client()
            result = await client.request_with_result(
                "get",
                url,
                context=context,
                raise_for_status=False,
                params=params,
            )
            if self._is_rate_limited(result):
                delay = self._rate_limit_delay(result)
                logger.warning(
                    "Inactive repository scan rate limited", delay=delay, **context
                )
                await self.sleep(delay)
                continue
            response = result.response
            if response is None:
                raise InactiveRepoScanError(
                    f"GitHub request failed for {url}: {result.error_type or 'request error'}"
                )
            if response.status_code == 401:
                if authentication_retried:
                    raise InactiveRepoScanError(
                        f"Persistent GitHub authentication failure for {url}"
                    )
                self.auth.invalidate()
                authentication_retried = True
                continue
            if response.status_code >= 500:
                if server_attempt >= SERVER_RETRIES:
                    raise InactiveRepoScanError(
                        f"GitHub server retries exhausted for {url}"
                    )
                delay = min(2**server_attempt, 2)
                server_attempt += 1
                await self.sleep(delay)
                continue
            if response.status_code == 451:
                raise GitHubLegalRestrictionError(
                    f"GitHub legally restricted access to {url}"
                )
            if not 200 <= response.status_code < 300:
                raise InactiveRepoScanError(
                    f"GitHub request failed with HTTP {response.status_code} for {url}"
                )
            try:
                return response.json()
            except ValueError as error:
                raise InactiveRepoScanError(
                    f"GitHub returned invalid JSON for {url}"
                ) from error

    async def _pages(
        self, url: str, *, params: dict[str, Any], context: dict[str, Any]
    ) -> AsyncGenerator[list[Any]]:
        page = 1
        while True:
            body = await self._request_json(
                url,
                params={**params, "per_page": PAGE_SIZE, "page": page},
                context={**context, "page": page},
            )
            if not isinstance(body, list):
                raise InactiveRepoScanError(
                    f"GitHub returned an unexpected paginated response for {url}"
                )
            yield body
            if len(body) < PAGE_SIZE:
                return
            page += 1

    async def _has_bot_pr_threshold(self, organization: str, repository: str) -> bool:
        count = 0
        url = f"https://api.github.com/repos/{organization}/{repository}/pulls"
        async for page in self._pages(
            url,
            params={"state": "open"},
            context={"organization": organization, "repository": repository},
        ):
            for pull_request in page:
                try:
                    login = pull_request["user"]["login"]
                except (KeyError, TypeError) as error:
                    raise InactiveRepoScanError(
                        f"GitHub returned an unexpected pull request for {organization}/{repository}"
                    ) from error
                if not isinstance(login, str):
                    raise InactiveRepoScanError(
                        f"GitHub returned an unexpected pull request for {organization}/{repository}"
                    )
                if login in BOT_AUTHORS:
                    count += 1
                    if count >= PR_THRESHOLD:
                        return True
        return False

    async def _latest_commit_time(
        self, organization: str, repository: str, default_branch: str
    ) -> datetime | None:
        url = f"https://api.github.com/repos/{organization}/{repository}/commits"
        body = await self._request_json(
            url,
            params={"sha": default_branch, "per_page": 1},
            context={"organization": organization, "repository": repository},
        )
        if not isinstance(body, list):
            raise InactiveRepoScanError(
                f"GitHub returned an unexpected commit response for {organization}/{repository}"
            )
        if not body:
            return None
        try:
            value = body[0]["commit"]["committer"]["date"]
            commit_time = datetime.fromisoformat(value)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            raise InactiveRepoScanError(
                f"GitHub returned an unexpected commit for {organization}/{repository}"
            ) from error
        if commit_time.tzinfo is None:
            raise InactiveRepoScanError(
                f"GitHub returned a commit without a timezone for {organization}/{repository}"
            )
        return commit_time.astimezone(UTC)

    async def scan(
        self,
        organization: str = "flathub",
        scan_started_at: datetime | None = None,
    ) -> InactiveRepoScanResult:
        started_at = scan_started_at or datetime.now(UTC)
        if started_at.tzinfo is None:
            raise ValueError("scan_started_at must include a timezone")
        started_at = started_at.astimezone(UTC)
        cutoff = started_at - timedelta(days=21)
        candidates: set[str] = set()
        repositories_seen = 0
        repositories_checked = 0
        repositories_at_pr_threshold = 0
        unobservable_repositories: set[str] = set()
        url = f"https://api.github.com/orgs/{organization}/repos"
        logger.info(
            "Inactive repository scan started",
            organization=organization,
            scan_started_at=started_at.isoformat(),
            cutoff=cutoff.isoformat(),
        )
        async for page in self._pages(
            url,
            params={"type": "public"},
            context={"organization": organization},
        ):
            for repository_data in page:
                repositories_seen += 1
                try:
                    name = repository_data["name"]
                    full_name = repository_data["full_name"]
                    owner = repository_data["owner"]["login"]
                    archived = repository_data["archived"]
                    private = repository_data["private"]
                except (KeyError, TypeError) as error:
                    raise InactiveRepoScanError(
                        "GitHub returned an unexpected repository response"
                    ) from error
                if (
                    not all(
                        isinstance(value, str) for value in (name, full_name, owner)
                    )
                    or not isinstance(archived, bool)
                    or not isinstance(private, bool)
                ):
                    raise InactiveRepoScanError(
                        "GitHub returned an unexpected repository response"
                    )
                if (
                    owner.casefold() != organization.casefold()
                    or full_name.casefold() != f"{organization}/{name}".casefold()
                    or private
                    or archived
                ):
                    continue
                default_branch = repository_data.get("default_branch")
                if not isinstance(default_branch, str) or not default_branch:
                    raise InactiveRepoScanError(
                        "GitHub returned an unexpected repository response"
                    )
                repositories_checked += 1
                try:
                    if not await self._has_bot_pr_threshold(organization, name):
                        continue
                    repositories_at_pr_threshold += 1
                    commit_time = await self._latest_commit_time(
                        organization, name, default_branch
                    )
                except GitHubLegalRestrictionError:
                    unobservable_repositories.add(name)
                    logger.warning(
                        "Inactive repository scan skipped legally restricted repository",
                        organization=organization,
                        repository=name,
                    )
                    continue
                if commit_time is not None and commit_time < cutoff:
                    candidates.add(name)
        completed_at = datetime.now(UTC)
        result = InactiveRepoScanResult(
            organization=organization,
            scan_started_at=started_at,
            scan_completed_at=completed_at,
            automatic_candidates=sorted(candidates),
            repositories_seen=repositories_seen,
            repositories_checked=repositories_checked,
            repositories_at_pr_threshold=repositories_at_pr_threshold,
            unobservable_repositories=sorted(unobservable_repositories),
        )
        logger.info(
            "Inactive repository scan completed",
            organization=organization,
            scan_completed_at=completed_at.isoformat(),
            repositories_seen=repositories_seen,
            repositories_checked=repositories_checked,
            repositories_at_pr_threshold=repositories_at_pr_threshold,
            automatic_candidates=len(candidates),
            repositories_unobservable=len(unobservable_repositories),
        )
        return result


async def publish_snapshot(db: AsyncSession, scan: InactiveRepoScanResult) -> bool:
    values = {
        "organization": scan.organization,
        "scan_started_at": scan.scan_started_at,
        "scan_completed_at": scan.scan_completed_at,
        "automatic_candidates": sorted(set(scan.automatic_candidates)),
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(InactiveRepoSnapshot).values(**values)
    elif dialect == "sqlite":
        statement = sqlite_insert(InactiveRepoSnapshot).values(**values)
    else:
        raise RuntimeError(f"Unsupported database dialect: {dialect}")
    statement = statement.on_conflict_do_update(
        index_elements=[InactiveRepoSnapshot.organization],
        set_={
            "scan_started_at": statement.excluded.scan_started_at,
            "scan_completed_at": statement.excluded.scan_completed_at,
            "automatic_candidates": statement.excluded.automatic_candidates,
        },
        where=InactiveRepoSnapshot.scan_started_at < statement.excluded.scan_started_at,
    ).returning(InactiveRepoSnapshot.organization)
    result = await db.execute(statement)
    return result.scalar_one_or_none() is not None
