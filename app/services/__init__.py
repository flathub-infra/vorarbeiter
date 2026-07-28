from .github_actions import GitHubActionsService
from .github_task import GitHubTaskService
from .merge import MergeService
from .pipeline import PipelineService
from .publishing import PublishingService

github_actions_service = GitHubActionsService()
github_task_service = GitHubTaskService()
merge_service = MergeService()
pipeline_service = PipelineService()
publishing_service = PublishingService()

__all__ = [
    "GitHubActionsService",
    "GitHubTaskService",
    "MergeService",
    "PipelineService",
    "PublishingService",
    "github_actions_service",
    "github_task_service",
    "merge_service",
    "pipeline_service",
    "publishing_service",
]
