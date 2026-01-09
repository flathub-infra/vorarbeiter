from app.routes.dashboard import dashboard_router
from app.routes.diffoscope import diffoscope_router
from app.routes.pipelines import pipelines_router
from app.routes.webhooks import webhooks_router

__all__ = [
    "dashboard_router",
    "diffoscope_router",
    "pipelines_router",
    "webhooks_router",
]
