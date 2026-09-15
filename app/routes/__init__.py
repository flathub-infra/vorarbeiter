from app.routes.dashboard import dashboard_router
from app.routes.diffoscope import diffoscope_router
from app.routes.inactive_repos import inactive_repos_router
from app.routes.merge import merge_router
from app.routes.pipelines import pipelines_router
from app.routes.webhooks import webhooks_router

__all__ = [
    "dashboard_router",
    "diffoscope_router",
    "inactive_repos_router",
    "merge_router",
    "pipelines_router",
    "webhooks_router",
]
