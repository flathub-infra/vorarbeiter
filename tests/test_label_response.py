import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx2 as httpx
import pytest

from app.config import settings
from app.main import app
from app.services.label_response import (
    LABEL_RESPONSES,
    MARKER_TEMPLATE,
    TOO_NEW_RESPONSE,
    respond_to_label,
)


class FakeGitHubClient:
    def __init__(self) -> None:
        self.comments: list[str] = []
        self.posts: list[str] = []
        self.post_retries: list[int | None] = []
        self.lookups = 0
        self.lookup_failures = 0
        self.lose_post_response = False
        self.post_lands = True
        self.page_size: int | None = None
        self.delay = 0.01

    async def request(self, method, url, **kwargs):
        assert method == "get"
        self.lookups += 1
        await asyncio.sleep(self.delay)
        if self.lookup_failures:
            self.lookup_failures -= 1
            return None
        comments = self.comments
        if self.page_size is not None:
            page = kwargs.get("params", {}).get("page", 1)
            start = (page - 1) * self.page_size
            comments = comments[start : start + self.page_size]
        response = MagicMock()
        response.json.return_value = [{"body": body} for body in comments]
        return response

    async def request_with_result(self, method, url, **kwargs):
        assert method == "post"
        await asyncio.sleep(self.delay)
        self.post_retries.append(kwargs.get("max_retries"))
        body = kwargs["json"]["body"]
        self.posts.append(body)
        if self.post_lands:
            self.comments.append(body)
        result = MagicMock()
        result.response = None if self.lose_post_response else MagicMock()
        return result


class FakeSession:
    def __init__(self, locks: dict[int, asyncio.Lock]) -> None:
        self._locks = locks
        self._held: asyncio.Lock | None = None
        self.statements: list[str] = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, statement, params):
        self.statements.append(str(statement))
        lock = self._locks.setdefault(params["key"], asyncio.Lock())
        await lock.acquire()
        self._held = lock


def fake_postgres_get_db(sessions: list[FakeSession] | None = None):
    locks: dict[int, asyncio.Lock] = {}

    @asynccontextmanager
    async def _get_db(*, use_replica: bool = False):
        session = FakeSession(locks)
        if sessions is not None:
            sessions.append(session)
        try:
            yield session
        finally:
            if session._held is not None:
                session._held.release()

    return _get_db


@pytest.mark.asyncio
async def test_posts_response_once_per_pr():
    client = FakeGitHubClient()

    with patch("app.utils.github.get_github_client", return_value=client):
        assert await respond_to_label("flathub/flathub", 123, "too-new") is True
        assert await respond_to_label("flathub/flathub", 123, "too-new") is True

    assert client.posts == [
        f"{TOO_NEW_RESPONSE}\n\n{MARKER_TEMPLATE.format(label='too-new')}"
    ]
    assert client.post_retries == [0]


@pytest.mark.asyncio
async def test_reapplied_label_does_not_repost():
    client = FakeGitHubClient()

    with patch("app.utils.github.get_github_client", return_value=client):
        await respond_to_label("flathub/flathub", 123, "too-new")
        await respond_to_label("flathub/flathub", 123, "too-new")

    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_response_text_change_does_not_repost(monkeypatch):
    client = FakeGitHubClient()

    with patch("app.utils.github.get_github_client", return_value=client):
        await respond_to_label("flathub/flathub", 123, "too-new")
        monkeypatch.setitem(LABEL_RESPONSES, "too-new", "Rewritten guidance.")
        assert await respond_to_label("flathub/flathub", 123, "too-new") is True

    assert len(client.posts) == 1
    assert client.posts[0].startswith(TOO_NEW_RESPONSE)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "edited"])
async def test_dedup_follows_the_comment_body(change):
    client = FakeGitHubClient()
    with patch("app.utils.github.get_github_client", return_value=client):
        await respond_to_label("flathub/flathub", 123, "too-new")
        if change == "deleted":
            client.comments.clear()
        else:
            client.comments[0] = "Edited by a maintainer."
        assert await respond_to_label("flathub/flathub", 123, "too-new") is True
    assert len(client.posts) == 2


@pytest.mark.asyncio
async def test_marker_on_a_later_comment_page_is_found():
    client = FakeGitHubClient()
    client.page_size = 100
    client.comments = ["filler"] * 100 + [
        f"{TOO_NEW_RESPONSE}\n\n{MARKER_TEMPLATE.format(label='too-new')}"
    ]

    with patch("app.utils.github.get_github_client", return_value=client):
        assert await respond_to_label("flathub/flathub", 123, "too-new") is True

    assert client.posts == []
    assert client.lookups == 2


@pytest.mark.asyncio
async def test_unrelated_label_posts_nothing():
    client = FakeGitHubClient()

    with patch("app.utils.github.get_github_client", return_value=client):
        assert await respond_to_label("flathub/flathub", 123, "needs-rebase") is False

    assert client.posts == []
    assert client.lookups == 0


@pytest.mark.asyncio
async def test_failed_lookup_retries_on_next_delivery():
    client = FakeGitHubClient()
    client.lookup_failures = 1

    with patch("app.utils.github.get_github_client", return_value=client):
        assert await respond_to_label("flathub/flathub", 123, "too-new") is False
        assert await respond_to_label("flathub/flathub", 123, "too-new") is True
        assert await respond_to_label("flathub/flathub", 123, "too-new") is True

    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_lost_post_response_is_confirmed_by_rescan():
    client = FakeGitHubClient()
    client.lose_post_response = True

    with patch("app.utils.github.get_github_client", return_value=client):
        assert await respond_to_label("flathub/flathub", 123, "too-new") is True
        assert await respond_to_label("flathub/flathub", 123, "too-new") is True

    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_post_that_never_lands_retries_on_next_delivery():
    client = FakeGitHubClient()
    client.lose_post_response = True
    client.post_lands = False

    with patch("app.utils.github.get_github_client", return_value=client):
        assert await respond_to_label("flathub/flathub", 123, "too-new") is False
        client.lose_post_response = False
        client.post_lands = True
        assert await respond_to_label("flathub/flathub", 123, "too-new") is True

    assert len(client.posts) == 2
    assert len(client.comments) == 1


@pytest.mark.asyncio
async def test_concurrent_deliveries_post_once():
    client = FakeGitHubClient()
    sessions: list[FakeSession] = []

    with (
        patch("app.utils.github.get_github_client", return_value=client),
        patch(
            "app.services.label_response.get_db",
            fake_postgres_get_db(sessions),
        ),
    ):
        results = await asyncio.gather(
            respond_to_label("flathub/flathub", 123, "too-new"),
            respond_to_label("flathub/flathub", 123, "too-new"),
        )

    assert results == [True, True]
    assert len(client.posts) == 1
    assert len(sessions) == 2
    assert all(
        any("pg_advisory_xact_lock" in statement for statement in session.statements)
        for session in sessions
    )


def labeled_payload(
    repo: str = "flathub/flathub",
    label: str = "too-new",
    state: str = "open",
    base: str = "new-pr",
) -> dict:
    return {
        "action": "labeled",
        "repository": {"full_name": repo},
        "sender": {"login": "test-actor"},
        "label": {"name": label},
        "pull_request": {
            "number": 123,
            "state": state,
            "head": {"ref": "new-app", "sha": "a" * 40},
            "base": {"ref": base},
        },
    }


async def deliver(payload: dict, delivery_id: str | None = None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        return await api.post(
            "/api/webhooks/github",
            json=payload,
            headers={"X-GitHub-Delivery": delivery_id or str(uuid.uuid4())},
        )


@pytest.mark.asyncio
async def test_webhook_posts_label_response_for_flathub_submission():
    with (
        patch.object(settings, "github_webhook_secret", ""),
        patch(
            "app.routes.webhooks.respond_to_label", AsyncMock(return_value=True)
        ) as respond,
    ):
        response = await deliver(labeled_payload())

    assert response.status_code == 202
    respond.assert_awaited_once_with("flathub/flathub", 123, "too-new")


@pytest.mark.asyncio
async def test_redelivered_webhook_posts_once():
    client = FakeGitHubClient()
    payload = labeled_payload()

    with (
        patch.object(settings, "github_webhook_secret", ""),
        patch("app.utils.github.get_github_client", return_value=client),
    ):
        delivery_id = str(uuid.uuid4())
        first = await deliver(payload, delivery_id)
        second = await deliver(payload, delivery_id)

    assert (first.status_code, second.status_code) == (202, 202)
    assert len(client.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        labeled_payload(label="needs-rebase"),
        labeled_payload(repo="flathub/org.test.App"),
        labeled_payload(state="closed"),
        labeled_payload(base="master"),
    ],
)
async def test_webhook_ignores_other_labels_and_repositories(payload):
    with (
        patch.object(settings, "github_webhook_secret", ""),
        patch("app.routes.webhooks.respond_to_label", AsyncMock()) as respond,
    ):
        response = await deliver(payload)

    assert response.status_code == 202
    respond.assert_not_awaited()
