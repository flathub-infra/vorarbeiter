import logging
import sys
from typing import Any

import structlog

from app.config import settings


def setup_logging() -> None:
    log_level = logging.DEBUG if settings.debug else logging.INFO

    logging.root.handlers = []

    renderer = (
        structlog.processors.JSONRenderer()
        if not settings.debug
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
        ],
        processor=renderer,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.setLevel(log_level)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(handler)

    for logger_name in [
        "_granian",
        "granian.access",
        "fastapi",
        "httpx",
        "gql",
    ]:
        logger = logging.getLogger(logger_name)
        logger.propagate = False
        logger.handlers = []
        logger.addHandler(handler)
        if logger_name == "gql":
            logger.setLevel(logging.WARNING)
        else:
            logger.setLevel(log_level)


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
