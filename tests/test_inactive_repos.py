import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx2 as httpx
import pytest
from sqlalchemy import select

from app.models import InactiveRepoSnapshot
from app.services.inactive_repos import (
    InactiveRepoScanError,
    InactiveRepoScanner,
    InactiveRepoScanResult,
    publish_snapshot,
)
from app.utils.github import GitHubAPIResult


def response(status: int, body, headers: dict[str, str] | None = None):
    return GitHubAPIResult(response=httpx.Response(status, json=body, headers=headers))


def repository(name: str, *, archived: bool = False):
    return {
        "name": name,
        "full_name": f"flathub/{name}",
        "owner": {"login": "flathub"},
        "archived": archived,
        "private": False,
        "default_branch": "main",
    }


def pull_request(author: str):
    return {"user": {"login": author}}


def commit(timestamp: datetime):
    return {"commit": {"committer": {"date": timestamp.isoformat()}}}


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    async def request_with_result(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs["params"]))
        result = self.handler(url, kwargs["params"])
        if isinstance(result, Exception):
            raise result
        return result


class FakeAuth:
    def __init__(self, *clients):
        self.clients = clients
        self.index = 0
        self.invalidations = 0

    async def get_client(self):
        return self.clients[self.index]

    def invalidate(self):
        self.invalidations += 1
        self.index = min(self.index + 1, len(self.clients) - 1)


@pytest.mark.asyncio
async def test_scan_classification_boundaries_and_pagination():
    started_at = datetime(2026, 9, 15, tzinfo=UTC)
    cutoff = started_at - timedelta(days=21)
    names = [
        "four",
        "five",
        "equal-cutoff",
        "recent-prs-old-commit",
        "no-commit",
        "later-page",
        "archived",
    ]
    bot_prs = [
        pull_request("flathubbot"),
        pull_request("dependabot[bot]"),
        pull_request("github-actions[bot]"),
        pull_request("flathubbot"),
        pull_request("dependabot[bot]"),
    ]

    def handler(url, params):
        if url.endswith("/orgs/flathub/repos"):
            return response(
                200,
                [repository(name, archived=name == "archived") for name in names],
            )
        name = url.split("/")[5]
        if url.endswith("/pulls"):
            if name == "four":
                return response(200, bot_prs[:4])
            if name == "later-page" and params["page"] == 1:
                return response(
                    200,
                    bot_prs[:3] + [pull_request("human")] * 97,
                )
            if name == "later-page":
                return response(200, bot_prs[3:])
            return response(200, bot_prs)
        if name == "no-commit":
            return response(200, [])
        if name == "equal-cutoff":
            return response(200, [commit(cutoff)])
        return response(200, [commit(cutoff - timedelta(seconds=1))])

    client = FakeClient(handler)
    result = await InactiveRepoScanner(auth=FakeAuth(client)).scan(
        scan_started_at=started_at
    )

    assert result.automatic_candidates == [
        "five",
        "later-page",
        "recent-prs-old-commit",
    ]
    assert result.repositories_seen == 7
    assert result.repositories_checked == 6
    assert result.repositories_at_pr_threshold == 5
    archived_requests = [
        request for request in client.requests if "/archived/" in request[1]
    ]
    assert archived_requests == []
    later_pages = [
        request[2]["page"]
        for request in client.requests
        if request[1].endswith("/later-page/pulls")
    ]
    assert later_pages == [1, 2]


@pytest.mark.asyncio
async def test_rate_limit_retries_same_page_and_refreshes_client_after_wait():
    started_at = datetime(2026, 9, 15, tzinfo=UTC)
    repo_result = response(200, [repository("limited")])
    rate_limit = GitHubAPIResult(
        response=None,
        should_queue=True,
        error_type="rate_limit",
        retry_after=0,
    )
    pull_result = response(200, [pull_request("human")])
    first_client = FakeClient(
        lambda url, params: repo_result if "/orgs/" in url else rate_limit
    )
    second_client = FakeClient(lambda url, params: pull_result)
    auth = FakeAuth(first_client, second_client)

    async def sleep(_delay):
        auth.index = 1

    result = await InactiveRepoScanner(auth=auth, sleep=sleep).scan(
        scan_started_at=started_at
    )

    assert result.automatic_candidates == []
    assert first_client.requests[-1][1:] == second_client.requests[0][1:]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
async def test_response_rate_limits_retry_same_request(status):
    started_at = datetime(2026, 9, 15, tzinfo=UTC)
    calls = 0

    def handler(url, params):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(
                status,
                {"message": "API rate limit exceeded"},
                {"Retry-After": "0"},
            )
        return response(200, [])

    client = FakeClient(handler)
    result = await InactiveRepoScanner(auth=FakeAuth(client), sleep=AsyncMock()).scan(
        scan_started_at=started_at
    )

    assert result.repositories_seen == 0
    assert client.requests[0] == client.requests[1]


@pytest.mark.asyncio
async def test_expired_installation_credential_retries_once():
    started_at = datetime(2026, 9, 15, tzinfo=UTC)
    expired_client = FakeClient(lambda url, params: response(401, {}))
    fresh_client = FakeClient(lambda url, params: response(200, []))
    auth = FakeAuth(expired_client, fresh_client)

    result = await InactiveRepoScanner(auth=auth).scan(scan_started_at=started_at)

    assert result.repositories_seen == 0
    assert auth.invalidations == 1
    assert expired_client.requests[0][1:] == fresh_client.requests[0][1:]


@pytest.mark.asyncio
async def test_persistent_authentication_failure_aborts():
    started_at = datetime(2026, 9, 15, tzinfo=UTC)
    expired_client = FakeClient(lambda url, params: response(401, {}))
    auth = FakeAuth(expired_client, expired_client)

    with pytest.raises(InactiveRepoScanError, match="Persistent GitHub authentication"):
        await InactiveRepoScanner(auth=auth).scan(scan_started_at=started_at)


@pytest.mark.asyncio
async def test_unexpected_commit_failure_aborts_scan():
    started_at = datetime(2026, 9, 15, tzinfo=UTC)

    def handler(url, params):
        if "/orgs/" in url:
            return response(200, [repository("broken")])
        if url.endswith("/pulls"):
            return response(200, [pull_request("flathubbot")] * 5)
        return response(500, {})

    scanner = InactiveRepoScanner(auth=FakeAuth(FakeClient(handler)), sleep=AsyncMock())

    with pytest.raises(InactiveRepoScanError, match="server retries exhausted"):
        await scanner.scan(scan_started_at=started_at)


@pytest.mark.asyncio
async def test_failed_scan_leaves_previous_snapshot_unchanged(db_session_maker):
    from scripts.cronjob import refresh_inactive_repos

    old_start = datetime(2026, 9, 1, tzinfo=UTC)
    async with db_session_maker() as db:
        db.add(
            InactiveRepoSnapshot(
                organization="flathub",
                scan_started_at=old_start,
                scan_completed_at=old_start,
                automatic_candidates=["old"],
            )
        )
        await db.commit()

    database_opened = False

    @asynccontextmanager
    async def get_db():
        nonlocal database_opened
        database_opened = True
        async with db_session_maker() as db:
            yield db

    scanner = Mock()
    scanner.scan = AsyncMock(side_effect=InactiveRepoScanError("mid-scan failure"))
    with (
        patch(
            "app.services.inactive_repos.InactiveRepoScanner",
            return_value=scanner,
        ),
        patch("app.database.get_db", get_db),
        pytest.raises(InactiveRepoScanError, match="mid-scan failure"),
    ):
        await refresh_inactive_repos()

    assert not database_opened
    async with db_session_maker() as db:
        snapshot = await db.scalar(select(InactiveRepoSnapshot))
        assert snapshot is not None
        assert snapshot.automatic_candidates == ["old"]


@pytest.mark.asyncio
async def test_complete_empty_scan_replaces_previous_snapshot(db_session_maker):
    old_start = datetime(2026, 9, 1, tzinfo=UTC)
    new_start = datetime(2026, 9, 15, tzinfo=UTC)
    async with db_session_maker() as db:
        db.add(
            InactiveRepoSnapshot(
                organization="flathub",
                scan_started_at=old_start,
                scan_completed_at=old_start,
                automatic_candidates=["old"],
            )
        )
        await db.commit()

    scan = InactiveRepoScanResult(
        organization="flathub",
        scan_started_at=new_start,
        scan_completed_at=new_start + timedelta(minutes=1),
        automatic_candidates=[],
        repositories_seen=0,
        repositories_checked=0,
        repositories_at_pr_threshold=0,
    )
    async with db_session_maker() as db:
        assert await publish_snapshot(db, scan)
        await db.commit()

    async with db_session_maker() as db:
        snapshot = await db.scalar(select(InactiveRepoSnapshot))
        assert snapshot is not None
        assert snapshot.scan_started_at.replace(tzinfo=UTC) == new_start
        assert snapshot.automatic_candidates == []


@pytest.mark.asyncio
async def test_older_overlapping_scan_cannot_replace_newer_snapshot(db_session_maker):
    older = datetime(2026, 9, 1, tzinfo=UTC)
    newer = datetime(2026, 9, 15, tzinfo=UTC)

    def scan(started_at, candidate):
        return InactiveRepoScanResult(
            organization="flathub",
            scan_started_at=started_at,
            scan_completed_at=started_at + timedelta(hours=1),
            automatic_candidates=[candidate],
            repositories_seen=1,
            repositories_checked=1,
            repositories_at_pr_threshold=1,
        )

    async def publish(value):
        async with db_session_maker() as db:
            published = await publish_snapshot(db, value)
            await db.commit()
            return published

    await asyncio.gather(publish(scan(older, "old")), publish(scan(newer, "new")))
    assert not await publish(scan(older, "old"))

    async with db_session_maker() as db:
        snapshot = await db.scalar(select(InactiveRepoSnapshot))
        assert snapshot is not None
        assert snapshot.scan_started_at.replace(tzinfo=UTC) == newer
        assert snapshot.automatic_candidates == ["new"]
