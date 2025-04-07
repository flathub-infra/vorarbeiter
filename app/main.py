from fastapi import FastAPI

from app.routes.webhooks import webhooks_router

app = FastAPI()


@app.get("/", tags=["health"])
async def read_root():
    return {"status": "ok"}


app.include_router(webhooks_router)
