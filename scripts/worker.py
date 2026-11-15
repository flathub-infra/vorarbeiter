#!/usr/bin/env python3

import asyncio
import sys
from pathlib import Path

import sentry_sdk
import structlog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.logger import setup_logging
from app.queue import app


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


async def run() -> None:
    async with app.open_async():
        await app.run_worker_async(
            queues=["build-diagnosis", "maintenance"],
            concurrency=2,
        )


def main() -> None:
    setup_observability()
    asyncio.run(run())


if __name__ == "__main__":
    main()
