from fastapi import FastAPI
import sentry_sdk

from app.config import settings
from app.routes import webhooks_router, pipelines_router

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=0.1,
        profiles_sample_rate=0.1,
    )

app = FastAPI()


@app.get("/", tags=["health"])
async def read_root():
    return {"status": "ok"}


app.include_router(webhooks_router)
app.include_router(pipelines_router)
