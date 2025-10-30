from contextlib import asynccontextmanager

import sentry_sdk
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.logger import setup_logging
from app.middleware import LoggingMiddleware
from app.routes import pipelines_router, webhooks_router

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup")
    yield
    logger.info("Application shutdown")


app = FastAPI(lifespan=lifespan)
origins = [
    "https://flathub.org",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex="http://localhost(:.*)?",
    allow_credentials=True,
    allow_headers=["*"],
)

app.add_middleware(LoggingMiddleware)


@app.get("/", tags=["health"])
async def read_root():
    return {"status": "ok"}


app.include_router(webhooks_router)
app.include_router(pipelines_router)
