import time
from typing import Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = structlog.get_logger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()

        try:
            response = await call_next(request)
            process_time = time.time() - start_time
            status_code = response.status_code

            logger.info(
                "HTTP Request",
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                duration=round(process_time * 1000, 2),
                client=request.client.host if request.client else None,
            )
            return response

        except Exception as exc:
            process_time = time.time() - start_time

            logger.error(
                "HTTP Request Exception",
                method=request.method,
                path=request.url.path,
                duration=round(process_time * 1000, 2),
                client=request.client.host if request.client else None,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
