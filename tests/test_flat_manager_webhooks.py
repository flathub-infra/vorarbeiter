import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch

import httpx2 as httpx
import pytest
import pytest_asyncio
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import FlatManagerWebhookDelivery
from app.queue import app as queue_app
from app.utils.flat_manager import JobKind, JobStatus, PublishedState, RepoState

KEY_FILE = "ZmxhdC1tYW5hZ2VyLXdlYmhvb2stdGVzdC1zZWNyZXQ=\n"
BODY = (
    b'{"id":18231,"type":"job.finished","created_at":"2026-11-16T09:30:00Z",'
    b'"data":{"job":{"id":624335,"kind":0,"status":2,"repo":null,'
    b'"results":"{\\"refs\\":{}}","start_after":null},'
    b'"build":{"id":198765,"repo":"stable","app_id":null,"repo_state":6,'
    b'"repo_state_reason":null,"published_state":0,'
    b'"published_state_reason":null,"commit_job_id":624335,'
    b'"publish_job_id":null}}}'
)
SIGNATURE = "sha256=02ea48ed7f50e59cc22493cf5983d2f0977e26424fd7b96d08e9c8a7d7ae57a1"


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 16, 9, 32, tzinfo=UTC)


def sign(body: bytes, key: bytes = b"flat-manager-webhook-test-secret") -> str:
    import hashlib
    import hmac

    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


@pytest.fixture
def connector(db_session_maker, tmp_path):
    @asynccontextmanager
    async def get_test_db(*, use_replica: bool = False):
        async with db_session_maker() as db:
            yield db
            await db.commit()

    key_file = tmp_path / "hmac-key"
    key_file.write_text(KEY_FILE)
    connector = InMemoryConnector()
    with (
        patch("app.services.flat_manager_events.get_db", get_test_db),
        patch.object(settings, "flat_manager_webhook_secret_file", str(key_file)),
        patch("app.routes.flat_manager_webhooks.datetime", FrozenDatetime),
        queue_app.replace_connector(connector),
    ):
        yield connector


@pytest_asyncio.fixture
async def api():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def post(api, body: bytes = BODY, signature: str = SIGNATURE):
    return await api.post(
        "/api/webhooks/flat-manager",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Flatmanager-Event": "job.finished",
            "X-Flatmanager-Signature": signature,
        },
    )


async def inbox(db_session_maker) -> list[FlatManagerWebhookDelivery]:
    async with db_session_maker() as db:
        return list((await db.execute(select(FlatManagerWebhookDelivery))).scalars())


def event_jobs(connector):
    return [
        job
        for job in connector.jobs.values()
        if job["task_name"] == "app.tasks.flat_manager_event"
    ]


def test_wire_integers_match_flat_manager():
    assert [k.value for k in JobKind] == list(range(6))
    assert [s.value for s in JobStatus] == list(range(4))
    assert [s.value for s in PublishedState] == list(range(4))
    assert {s.name: s.value for s in RepoState} == {
        "UPLOADING": 0,
        "COMMITTING": 1,
        "READY": 2,
        "FAILED": 3,
        "PURGING": 4,
        "PURGED": 5,
        "VALIDATING": 6,
    }


@pytest.mark.asyncio
async def test_test_vector_is_accepted_and_enqueued(connector, api, db_session_maker):
    response = await post(api)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    [row] = await inbox(db_session_maker)
    assert row.id == 18231
    assert row.event_type == "job.finished"
    assert row.payload["data"]["build"]["repo_state"] == 6
    assert row.processed_at is None
    [job] = event_jobs(connector)
    assert job["queue_name"] == "flat-manager"
    assert job["args"] == {"delivery_id": 18231}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "signature"),
    [
        (BODY.replace(b"624335", b"624336", 1), SIGNATURE),
        (BODY, sign(BODY, b"another-secret")),
        (BODY, ""),
    ],
    ids=["altered-body", "altered-key", "missing"],
)
async def test_bad_signature_is_rejected(
    connector, api, db_session_maker, body, signature
):
    response = await post(api, body, signature)

    assert response.status_code == 401
    assert await inbox(db_session_maker) == []
    assert event_jobs(connector) == []


@pytest.mark.asyncio
async def test_stale_timestamp_is_rejected(connector, api, db_session_maker):
    body = BODY.replace(b"09:30:00Z", b"09:26:59Z")

    response = await post(api, body, sign(body))

    assert response.status_code == 400
    assert await inbox(db_session_maker) == []


@pytest.mark.asyncio
async def test_duplicate_delivery_enqueues_once(connector, api, db_session_maker):
    first = await post(api)
    second = await post(api)

    assert first.json() == second.json() == {"status": "accepted"}
    assert len(await inbox(db_session_maker)) == 1
    assert len(event_jobs(connector)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        BODY.replace(b'"kind":0', b'"kind":9'),
        BODY.replace(b'"job":{"id":624335,', b'"job":{'),
    ],
    ids=["invalid-json", "unknown-kind", "missing-job-id"],
)
async def test_malformed_payload_is_rejected(connector, api, db_session_maker, body):
    response = await post(api, body, sign(body))

    assert response.status_code == 400
    assert await inbox(db_session_maker) == []


@pytest.mark.asyncio
async def test_oversized_body_is_rejected(connector, api):
    body = b" " * (64 * 1024 + 1)

    response = await post(api, body, sign(body))

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_null_build_is_accepted(connector, api, db_session_maker):
    payload = json.loads(BODY)
    payload["data"]["job"].update(kind=2, results=None)
    payload["data"]["build"] = None
    body = json.dumps(payload).encode()

    response = await post(api, body, sign(body))

    assert response.json() == {"status": "accepted"}
    [row] = await inbox(db_session_maker)
    assert row.payload["data"]["build"] is None


@pytest.mark.asyncio
async def test_unknown_event_type_is_ignored(connector, api, db_session_maker):
    body = BODY.replace(b'"job.finished"', b'"job.started"')

    response = await post(api, body, sign(body))

    assert response.json() == {"status": "ignored"}
    assert await inbox(db_session_maker) == []
    assert event_jobs(connector) == []


@pytest.mark.asyncio
async def test_unconfigured_receiver_refuses(connector, api):
    with patch.object(settings, "flat_manager_webhook_secret_file", None):
        response = await post(api)

    assert response.status_code == 503
