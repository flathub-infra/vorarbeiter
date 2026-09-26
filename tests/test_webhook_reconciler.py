import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import pytest

from app.models import Pipeline, PipelineTrigger
from app.models.webhook_event import WebhookEvent, WebhookSource
from app.services.webhook_reconciler import MAX_ATTEMPTS, WebhookDeliveryReconciler
from app.utils.github import GitHubAPIResult

HOOK_ID = 534439114
DELIVERIES = f"https://api.github.com/orgs/flathub/hooks/{HOOK_ID}/deliveries"
REF_URL = "https://api.github.com/repos/flathub/dev.dergs.Tonearm/git/ref/heads/master"
NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
GUID = "f7d2e9a2-b852-11f1-9b58-d15f25397fce"
OLD_SHA = "f69a5d2272b8496685bbf556f65f91046daf20bc"
NEW_SHA = "a4de28150b9d2e44dc8e9095c561d2ce3782f695"


def response(status: int, body, headers: dict[str, str] | None = None):
    return GitHubAPIResult(response=httpx.Response(status, json=body, headers=headers))


def delivery(delivery_id: int, *, guid=GUID, event="push", age=timedelta(hours=1)):
    delivered_at = (NOW - age).isoformat().replace("+00:00", "Z")
    return {
        "id": delivery_id,
        "guid": guid,
        "event": event,
        "delivered_at": delivered_at,
        "status_code": 500,
        "redelivery": False,
    }


def push_payload(ref="refs/heads/master", after=NEW_SHA):
    return {
        "ref": ref,
        "before": OLD_SHA,
        "after": after,
        "commits": [{"id": after}],
        "repository": {"full_name": "flathub/dev.dergs.Tonearm"},
        "sender": {"login": "nilathedragon"},
    }


class FakeGitHub:
    """Serves deliveries listings, delivery details, refs and redeliveries."""

    def __init__(
        self,
        *,
        failed=None,
        succeeded=None,
        payload=None,
        head_sha=NEW_SHA,
        ref_status=200,
        redelivery_status=202,
    ):
        self.failed = failed or []
        self.succeeded = succeeded or []
        self.payload = payload if payload is not None else push_payload()
        self.head_sha = head_sha
        self.ref_status = ref_status
        self.redelivery_status = redelivery_status
        self.requests = []

    @property
    def redeliveries(self):
        return [url for method, url in self.requests if method == "post"]

    async def request_with_result(self, method, url, **kwargs):
        self.requests.append((method, url))
        params = kwargs.get("params") or {}
        if url == DELIVERIES:
            listing = self.failed if params["status"] == "failure" else self.succeeded
            return response(200, listing)
        if method == "post" and url.endswith("/attempts"):
            return response(self.redelivery_status, {})
        if url.startswith(f"{DELIVERIES}/"):
            return response(200, {"request": {"payload": self.payload}})
        if url == REF_URL:
            if self.ref_status != 200:
                return response(self.ref_status, {"message": "Not Found"})
            return response(200, {"object": {"sha": self.head_sha}})
        raise AssertionError(f"Unexpected request {method} {url}")


class FakeAuth:
    def __init__(self, client):
        self.client = client

    async def get_client(self):
        return self.client

    def invalidate(self):
        pass


@pytest.fixture
def db(db_session_maker):
    @asynccontextmanager
    async def get_db(*, use_replica: bool = False):
        async with db_session_maker() as session:
            yield session

    with patch("app.services.webhook_reconciler.get_db", get_db):
        yield db_session_maker


@pytest.fixture
def sentry():
    with patch("app.services.webhook_reconciler.sentry_sdk") as sentry_sdk:
        yield sentry_sdk


async def reconcile(github: FakeGitHub):
    reconciler = WebhookDeliveryReconciler(
        HOOK_ID, auth=FakeAuth(github), sleep=AsyncMock()
    )
    return await reconciler.reconcile(now=NOW)


@pytest.mark.asyncio
async def test_failed_current_push_is_redelivered(db, sentry):
    github = FakeGitHub(failed=[delivery(1)])

    result = await reconcile(github)

    assert result.redelivered == [GUID]
    assert github.redeliveries == [f"{DELIVERIES}/1/attempts"]
    sentry.capture_message.assert_called_once()


@pytest.mark.asyncio
async def test_no_failed_pushes_skips_success_listing(db, sentry):
    github = FakeGitHub(failed=[delivery(1, event="pull_request")])

    result = await reconcile(github)

    assert result.failed_push_deliveries == 0
    assert github.requests == [("get", DELIVERIES)]


@pytest.mark.asyncio
async def test_guid_with_successful_redelivery_is_ignored(db, sentry):
    github = FakeGitHub(failed=[delivery(1)], succeeded=[delivery(2)])

    result = await reconcile(github)

    assert result.failed_push_deliveries == 0
    assert github.redeliveries == []


@pytest.mark.asyncio
async def test_already_stored_event_is_skipped(db, sentry):
    async with db() as session:
        session.add(
            WebhookEvent(
                id=uuid.UUID(GUID),
                source=WebhookSource.GITHUB,
                payload=push_payload(),
                repository="flathub/dev.dergs.Tonearm",
                actor="nilathedragon",
            )
        )
        await session.commit()
    github = FakeGitHub(failed=[delivery(1)])

    result = await reconcile(github)

    assert result.already_stored == [GUID]
    assert github.redeliveries == []


@pytest.mark.asyncio
async def test_push_superseded_by_newer_head_is_skipped(db, sentry):
    github = FakeGitHub(failed=[delivery(1)], head_sha="b" * 40)

    result = await reconcile(github)

    assert result.stale == [GUID]
    assert github.redeliveries == []


@pytest.mark.asyncio
async def test_push_to_deleted_branch_is_skipped(db, sentry):
    github = FakeGitHub(failed=[delivery(1)], ref_status=404)

    result = await reconcile(github)

    assert result.stale == [GUID]
    assert github.redeliveries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        push_payload(ref="refs/heads/patch-v1.5.1"),
        push_payload(after="0" * 40),
    ],
    ids=["untracked-branch", "branch-deletion"],
)
async def test_untracked_pushes_are_ignored(db, sentry, payload):
    github = FakeGitHub(failed=[delivery(1)], payload=payload)

    result = await reconcile(github)

    assert result.not_tracked == [GUID]
    assert github.redeliveries == []


@pytest.mark.asyncio
async def test_existing_pipeline_for_sha_is_skipped(db, sentry):
    async with db() as session:
        session.add(
            Pipeline(
                app_id="dev.dergs.Tonearm",
                params={
                    "repo": "flathub/dev.dergs.Tonearm",
                    "ref": "refs/heads/master",
                    "sha": NEW_SHA,
                },
                triggered_by=PipelineTrigger.MANUAL,
            )
        )
        await session.commit()
    github = FakeGitHub(failed=[delivery(1)])

    result = await reconcile(github)

    assert result.pipeline_exists == [GUID]
    assert github.redeliveries == []


@pytest.mark.asyncio
async def test_exhausted_attempts_alert_without_redelivery(db, sentry):
    attempts = [delivery(i, age=timedelta(hours=i)) for i in range(1, MAX_ATTEMPTS + 1)]
    github = FakeGitHub(failed=attempts)

    result = await reconcile(github)

    assert result.exhausted == [GUID]
    assert github.redeliveries == []
    # The newest attempt carries the payload.
    assert ("get", f"{DELIVERIES}/1") in github.requests
    sentry.capture_message.assert_called_once()


@pytest.mark.asyncio
async def test_redelivery_failure_is_reported(db, sentry):
    github = FakeGitHub(failed=[delivery(1)], redelivery_status=422)

    result = await reconcile(github)

    assert result.errors == [GUID]
    assert result.redelivered == []
    sentry.capture_message.assert_called_once()


@pytest.mark.asyncio
async def test_listing_follows_cursor_until_lookback():
    next_url = f"{DELIVERIES}?per_page=100&status=failure&cursor=abc"
    pages = {
        DELIVERIES: response(
            200,
            [delivery(3, guid="a", event="ping")],
            headers={"Link": f'<{next_url}>; rel="next"'},
        ),
        next_url: response(
            200,
            [
                delivery(2, guid="b", event="ping", age=timedelta(hours=23)),
                delivery(1, guid="c", event="ping", age=timedelta(hours=25)),
            ],
            headers={"Link": f'<{DELIVERIES}?cursor=never>; rel="next"'},
        ),
    }
    requested = []

    class Client:
        async def request_with_result(self, method, url, **kwargs):
            requested.append((url, kwargs.get("params")))
            return pages[url]

    reconciler = WebhookDeliveryReconciler(HOOK_ID, auth=FakeAuth(Client()))

    deliveries = await reconciler._list_deliveries("failure", NOW - timedelta(hours=24))

    assert [d["guid"] for d in deliveries] == ["a", "b"]
    assert requested == [
        (DELIVERIES, {"per_page": 100, "status": "failure"}),
        (next_url, None),
    ]
