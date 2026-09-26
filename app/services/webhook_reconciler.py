import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sentry_sdk
import structlog
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models import Pipeline
from app.models.webhook_event import WebhookEvent
from app.routes.webhooks import should_store_event
from app.utils.github_app import (
    GitHubAppInstallationAuth,
    GitHubAppRequestError,
    InstallationAuth,
    request_with_installation_auth,
)

logger = structlog.get_logger(__name__)

ORGANIZATION = "flathub"
PAGE_SIZE = 100
MAX_ATTEMPTS = 3
DEFAULT_LOOKBACK = timedelta(hours=24)

RATE_LIMIT_MAX_ATTEMPTS = 3
RATE_LIMIT_MAX_TOTAL_DELAY = 900.0


@dataclass
class ReconcileResult:
    failed_push_deliveries: int = 0
    redelivered: list[str] = field(default_factory=list)
    already_stored: list[str] = field(default_factory=list)
    not_tracked: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    pipeline_exists: list[str] = field(default_factory=list)
    exhausted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "failed_push_deliveries": self.failed_push_deliveries,
            "redelivered": self.redelivered,
            "already_stored": len(self.already_stored),
            "not_tracked": len(self.not_tracked),
            "stale": self.stale,
            "pipeline_exists": len(self.pipeline_exists),
            "exhausted": self.exhausted,
            "errors": self.errors,
        }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _alert(message: str, **context: Any) -> None:
    try:
        sentry_sdk.capture_message(
            message, level="warning", contexts={"webhook_delivery": context}
        )
    except Exception:
        logger.exception("Failed to report webhook delivery alert")


class WebhookDeliveryReconciler:
    def __init__(
        self,
        hook_id: int,
        auth: InstallationAuth | None = None,
        lookback: timedelta = DEFAULT_LOOKBACK,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.hook_id = hook_id
        self.auth = auth or GitHubAppInstallationAuth()
        self.lookback = lookback
        self.sleep = sleep
        self.deliveries_url = (
            f"https://api.github.com/orgs/{ORGANIZATION}/hooks/{hook_id}/deliveries"
        )

    async def _request(self, method: str, url: str, **kwargs: Any):
        return await request_with_installation_auth(
            self.auth,
            method,
            url,
            context={"hook_id": self.hook_id},
            sleep=self.sleep,
            max_rate_limit_attempts=RATE_LIMIT_MAX_ATTEMPTS,
            max_rate_limit_total_delay=RATE_LIMIT_MAX_TOTAL_DELAY,
            **kwargs,
        )

    async def _list_deliveries(self, status: str, since: datetime) -> list[dict]:
        deliveries: list[dict] = []
        url: str | None = self.deliveries_url
        params: dict[str, Any] | None = {"per_page": PAGE_SIZE, "status": status}
        while url:
            response = await self._request("get", url, params=params)
            page = response.json()
            if not isinstance(page, list):
                raise GitHubAppRequestError(
                    f"GitHub returned an unexpected delivery listing for {url}"
                )
            reached_window_start = False
            for delivery in page:
                delivered_at = _parse_time(delivery.get("delivered_at"))
                if delivered_at is None:
                    continue
                if delivered_at < since:
                    reached_window_start = True
                    continue
                deliveries.append(delivery)
            if reached_window_start:
                break
            url = response.links.get("next", {}).get("url")
            params = None
        return deliveries

    async def reconcile(self, now: datetime | None = None) -> ReconcileResult:
        since = (now or datetime.now(UTC)) - self.lookback
        result = ReconcileResult()

        failed = await self._list_deliveries("failure", since)
        failed_attempts: dict[str, list[dict]] = {}
        for delivery in failed:
            guid = delivery.get("guid")
            if delivery.get("event") == "push" and isinstance(guid, str):
                failed_attempts.setdefault(guid, []).append(delivery)
        if not failed_attempts:
            return result

        succeeded = {
            delivery.get("guid")
            for delivery in await self._list_deliveries("success", since)
        }
        for guid, attempts in failed_attempts.items():
            if guid in succeeded:
                continue
            result.failed_push_deliveries += 1
            try:
                await self._reconcile_delivery(guid, attempts, result)
            except Exception as error:
                logger.exception(
                    "Failed to reconcile webhook delivery", guid=guid, error=str(error)
                )
                _alert(
                    "Failed to reconcile webhook delivery",
                    guid=guid,
                    error=str(error),
                )
                result.errors.append(guid)

        logger.info("Webhook delivery reconciliation finished", **result.summary())
        return result

    async def _reconcile_delivery(
        self, guid: str, attempts: list[dict], result: ReconcileResult
    ) -> None:
        try:
            event_id = uuid.UUID(guid)
        except ValueError:
            event_id = None
        if event_id is not None:
            async with get_db() as db:
                if await db.get(WebhookEvent, event_id) is not None:
                    result.already_stored.append(guid)
                    return

        attempts.sort(key=lambda d: d.get("delivered_at") or "", reverse=True)
        delivery_id = attempts[0]["id"]
        detail = (
            await self._request("get", f"{self.deliveries_url}/{delivery_id}")
        ).json()
        payload = (detail.get("request") or {}).get("payload") or {}
        if not isinstance(payload, dict) or not should_store_event(payload):
            result.not_tracked.append(guid)
            return

        repository = payload["repository"]["full_name"]
        ref = payload["ref"]
        after = payload["after"]
        context = {
            "guid": guid,
            "delivery_id": delivery_id,
            "repository": repository,
            "ref": ref,
            "sha": after,
            "attempts": len(attempts),
        }

        if len(attempts) >= MAX_ATTEMPTS:
            logger.error("Webhook delivery redelivery attempts exhausted", **context)
            _alert("Webhook delivery redelivery attempts exhausted", **context)
            result.exhausted.append(guid)
            return

        try:
            ref_response = await self._request(
                "get",
                f"https://api.github.com/repos/{repository}/git/ref/"
                f"{ref.removeprefix('refs/')}",
            )
        except GitHubAppRequestError as error:
            if error.status_code != 404:
                raise
            logger.info("Skipping webhook delivery for deleted ref", **context)
            result.stale.append(guid)
            return
        current_sha = (ref_response.json().get("object") or {}).get("sha")
        if current_sha != after:
            logger.info(
                "Skipping stale webhook delivery", current_sha=current_sha, **context
            )
            result.stale.append(guid)
            return

        async with get_db() as db:
            existing = await db.execute(
                select(Pipeline.id)
                .where(
                    Pipeline.params["repo"].as_string() == repository,
                    Pipeline.params["ref"].as_string() == ref,
                    Pipeline.params["sha"].as_string() == after,
                )
                .limit(1)
            )
            if existing.first() is not None:
                result.pipeline_exists.append(guid)
                return

        await self._request("post", f"{self.deliveries_url}/{delivery_id}/attempts")
        logger.warning("Redelivered failed webhook delivery", **context)
        _alert("Redelivered failed webhook delivery", **context)
        result.redelivered.append(guid)


async def reconcile_webhook_deliveries() -> dict[str, Any]:
    if settings.github_webhook_hook_id is None:
        logger.info("GITHUB_WEBHOOK_HOOK_ID is not set, skipping reconciliation")
        return {"status": "skipped", "reason": "GITHUB_WEBHOOK_HOOK_ID is not set"}
    result = await WebhookDeliveryReconciler(
        settings.github_webhook_hook_id
    ).reconcile()
    return {"status": "completed", **result.summary()}
