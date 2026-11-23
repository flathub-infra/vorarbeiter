#!/usr/bin/env python3

import asyncio
import signal
import sys
from pathlib import Path

import sentry_sdk
import structlog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.logger import setup_logging
from app.queue import app
from app.services import build_diagnosis


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


SLOW_QUEUES = ["build-diagnosis"]
FAST_QUEUES = ["builds", "github", "permission", "maintenance", "flat-manager"]
SLOW_CONCURRENCY = 2


async def serve_health(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    await reader.readline()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
    await writer.drain()
    writer.close()


async def run() -> None:
    options = {"install_signal_handlers": False}
    async with app.open_async():
        health = await asyncio.start_server(
            serve_health, "0.0.0.0", settings.worker_health_port
        )
        workers = [
            asyncio.create_task(
                app.run_worker_async(
                    queues=SLOW_QUEUES, concurrency=SLOW_CONCURRENCY, **options
                )
            ),
            asyncio.create_task(
                app.run_worker_async(
                    queues=FAST_QUEUES,
                    concurrency=settings.worker_fast_concurrency,
                    **options,
                )
            ),
        ]

        def stop() -> None:
            for worker in workers:
                worker.cancel()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop)
        await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
        stop()
        await asyncio.wait(workers)
        health.close()
        for worker in workers:
            if not worker.cancelled() and (error := worker.exception()):
                raise error


def main() -> None:
    missing = [
        name.upper()
        for name in build_diagnosis.REQUIRED_SETTINGS
        if not getattr(settings, name)
    ]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)}")
    setup_observability()
    asyncio.run(run())


if __name__ == "__main__":
    main()
