import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

from app.services.github_actions import GitHubActionsService
from app.utils.github import GitHubAPIClient

JOB_DATA: dict[str, Any] = {
    "app_id": "org.example.App",
    "params": {
        "owner": "flathub-infra",
        "repo": "vorarbeiter",
        "workflow_id": "build.yml",
        "inputs": {
            "callback_token": "secret-callback",
            "flat_manager_token": "secret-upload",
        },
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,message,attempts",
    [
        (422, 'Unexpected inputs provided: ["linter_review_report"]', 1),
        (403, "API rate limit exceeded", 1),
        (429, "Too many requests", 1),
        (503, "Service unavailable", 4),
    ],
)
async def test_dispatch_preserves_github_failure(status, message, attempts, caplog):
    request = httpx.Request(
        "POST",
        "https://api.github.com/dispatches",
        headers={"Authorization": "Bearer secret-auth"},
        json=JOB_DATA["params"]["inputs"],
    )
    response = httpx.Response(
        status,
        request=request,
        json={"message": message, "echo": "secret-callback secret-upload secret-auth"},
        headers={"x-github-request-id": "test-request-id"},
    )
    with (
        patch(
            "httpx2.AsyncClient.request", new=AsyncMock(return_value=response)
        ) as send,
        patch("app.utils.github.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(httpx.HTTPStatusError) as exc,
    ):
        await GitHubActionsService().dispatch("job", "pipeline", JOB_DATA)

    assert exc.value.response is response
    assert exc.value.request is request
    assert exc.value.response.json()["message"] == message
    assert send.call_count == attempts
    assert "test-request-id" in caplog.text
    assert str(status) in str(exc.value)
    for secret in ("secret-auth", "secret-callback", "secret-upload"):
        assert secret not in caplog.text
        assert secret not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type,attempts", [(httpx.ReadTimeout, 4), (RuntimeError, 1)]
)
async def test_dispatch_preserves_non_http_failure(error_type, attempts):
    error = error_type("original failure")
    with (
        patch("httpx2.AsyncClient.request", new=AsyncMock(side_effect=error)) as send,
        patch("app.utils.github.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(error_type) as exc,
    ):
        await GitHubActionsService().dispatch("job", "pipeline", JOB_DATA)

    assert exc.value is error
    assert send.call_count == attempts


@pytest.mark.asyncio
async def test_dispatch_accepts_empty_204_response():
    response = httpx.Response(
        204, request=httpx.Request("POST", "https://api.github.com/dispatches")
    )
    with patch(
        "httpx2.AsyncClient.request", new=AsyncMock(return_value=response)
    ) as send:
        result = await GitHubActionsService().dispatch("job", "pipeline", JOB_DATA)

    assert result == {
        "status": "dispatched",
        "job_id": "job",
        "pipeline_id": "pipeline",
        "owner": "flathub-infra",
        "repo": "vorarbeiter",
        "workflow_id": "build.yml",
        "ref": "main",
    }
    send.assert_called_once()
    assert json.loads(send.call_args.kwargs["content"]) == {
        "ref": "main",
        "inputs": {**JOB_DATA["params"]["inputs"], "app_id": "org.example.App"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,message,should_queue",
    [
        (422, "Invalid inputs", False),
        (403, "API rate limit exceeded", True),
        (503, "Service unavailable", True),
    ],
)
async def test_helper_still_returns_results_by_default(status, message, should_queue):
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.github.com/dispatches"),
        json={"message": message},
    )
    with patch("httpx2.AsyncClient.request", new=AsyncMock(return_value=response)):
        result = await GitHubAPIClient("token").request_with_result(
            "post", str(response.url), max_retries=0
        )

    assert result.response is None
    assert result.should_queue is should_queue


@pytest.mark.asyncio
async def test_dispatch_failure_reason_reaches_sentry_without_credentials():
    events = []

    def capture(event, hint):
        events.append(event)

    class DiscardTransport(Transport):
        def capture_envelope(self, envelope):
            pass

    request = httpx.Request(
        "POST",
        "https://api.github.com/dispatches",
        headers={"Authorization": "Bearer secret-auth"},
        json=JOB_DATA["params"]["inputs"],
    )
    response = httpx.Response(
        422,
        request=request,
        json={
            "message": 'Unexpected inputs provided: ["linter_review_report"] '
            "secret-auth secret-callback secret-upload " + "x" * 1000,
            "other": "unrelated-response-body",
        },
    )
    with (
        sentry_sdk.isolation_scope() as scope,
        patch("httpx2.AsyncClient.request", new=AsyncMock(return_value=response)),
    ):
        scope.set_client(
            sentry_sdk.Client(
                dsn="https://public@example.invalid/1",
                transport=DiscardTransport(),
                before_send=capture,
                # Test emitted diagnostics independently of stack-local collection.
                include_local_variables=False,
            )
        )
        try:
            await GitHubActionsService().dispatch("job", "pipeline", JOB_DATA)
        except httpx.HTTPStatusError as error:
            sentry_sdk.capture_exception(error)

    assert len(events) == 1
    serialized = json.dumps(events[0])
    assert "Unexpected inputs provided" in serialized
    assert "linter_review_report" in serialized
    assert "x" * 501 not in serialized
    for value in (
        "secret-auth",
        "secret-callback",
        "secret-upload",
        "unrelated-response-body",
    ):
        assert value not in serialized


@pytest.mark.parametrize(
    "body", [b"not JSON", b"[]", b'{"message": null}', b'{"message": {}}']
)
def test_invalid_github_error_body_does_not_mask_failure(body):
    response = httpx.Response(
        502,
        content=body,
        request=httpx.Request("POST", "https://api.github.com/dispatches"),
    )
    assert GitHubAPIClient("token")._safe_error_message(response) is None


def test_github_error_message_redacts_before_truncating():
    secret = 'quoted"secret\\value'
    response = httpx.Response(
        422,
        request=httpx.Request(
            "POST",
            "https://api.github.com/dispatches",
            json={"inputs": {"callback_token": secret}},
        ),
        json={"message": "x" * 490 + json.dumps(secret)[1:-1]},
    )
    message = GitHubAPIClient("token")._safe_error_message(response)
    assert message == "x" * 490 + "[REDACTED]"
