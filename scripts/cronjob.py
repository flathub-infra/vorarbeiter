#!/usr/bin/env python3

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpxyz as httpx
import sentry_sdk
import structlog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.logger import setup_logging

COMMANDS = (
    "publish",
    "check-jobs",
    "cleanup-stale",
    "github-tasks-process",
    "github-tasks-cleanup",
    "prune-beta",
    "prune-stable",
)


def setup_observability() -> None:
    setup_logging()
    logger = structlog.get_logger(__name__)

    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=0.1,
            profiles_sample_rate=0.1,
            enable_tracing=True,
        )
        logger.info("Sentry integration initialized", dsn=settings.sentry_dsn)


async def publish_pipelines() -> dict[str, Any]:
    from app.database import get_db
    from app.services import publishing_service

    async with get_db() as db:
        result = await publishing_service.publish_pipelines(db)

    return {
        "published": result.published,
        "superseded": result.superseded,
        "errors": result.errors,
    }


async def check_jobs() -> dict[str, Any]:
    from app.database import get_db
    from app.services.job_monitor import JobMonitor

    async with get_db() as db:
        result = await JobMonitor(db=db).check_all_active_pipelines(db)

    return {
        "status": "completed",
        **result,
    }


async def cleanup_stale() -> dict[str, Any]:
    from app.database import get_db
    from app.services import pipeline_service

    async with get_db() as db:
        cancelled_ids = await pipeline_service.cancel_stale_running_pipelines(db)

    return {
        "status": "completed",
        "cancelled_pipelines": len(cancelled_ids),
        "cancelled_pipeline_ids": cancelled_ids,
    }


async def process_github_tasks() -> dict[str, Any]:
    from app.database import get_db
    from app.services import github_task_service

    async with get_db() as db:
        processed = await github_task_service.process_pending_tasks(db)

    return {
        "status": "completed",
        "processed_tasks": processed,
    }


async def cleanup_github_tasks() -> dict[str, Any]:
    from app.database import get_db
    from app.services import github_task_service

    async with get_db() as db:
        deleted = await github_task_service.cleanup_old_tasks(db)

    return {
        "status": "completed",
        "deleted_tasks": deleted,
    }


async def prune_repo(repo: str) -> dict[str, Any]:
    url = f"{settings.flat_manager_url}/api/v1/prune"
    headers = {"Authorization": f"Bearer {settings.flat_manager_token}"}

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(url, headers=headers, json={"repo": repo})
        if response.status_code == 504:
            return {
                "status": "completed",
                "status_code": 504,
                "detail": response.text,
            }
        response.raise_for_status()

    result = response.json()
    if isinstance(result, dict):
        return result

    return {"result": result}


async def close_flat_manager_client() -> None:
    from app.utils import flat_manager

    if flat_manager._client is None:
        return

    await flat_manager._client.close()
    flat_manager._client = None


async def dispatch(command: str) -> dict[str, Any]:
    match command:
        case "publish":
            return await publish_pipelines()
        case "check-jobs":
            return await check_jobs()
        case "cleanup-stale":
            return await cleanup_stale()
        case "github-tasks-process":
            return await process_github_tasks()
        case "github-tasks-cleanup":
            return await cleanup_github_tasks()
        case "prune-beta":
            return await prune_repo("beta")
        case "prune-stable":
            return await prune_repo("stable")

    raise ValueError(f"Unknown command: {command}")


async def run(command: str) -> dict[str, Any]:
    try:
        return await dispatch(command)
    finally:
        await close_flat_manager_client()


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute pipeline operations")
    parser.add_argument(
        "command",
        choices=COMMANDS,
        help="Which operation to run",
    )

    args = parser.parse_args()
    setup_observability()
    result = asyncio.run(run(args.command))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
