from app.providers.github import GitHubProvider

# Create a single GitHub provider instance
github_provider = GitHubProvider()

__all__ = ["GitHubProvider", "github_provider"]
