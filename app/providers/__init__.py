from app.providers.base import JobProvider, ProviderType
from app.providers.github import GitHubJobProvider

_providers = {
    ProviderType.GITHUB: GitHubJobProvider(),
}


async def initialize_providers():
    for provider in _providers.values():
        await provider.initialize()


def get_provider(provider_type: ProviderType) -> JobProvider:
    if provider_type not in _providers:
        raise ValueError(f"Unsupported provider type: {provider_type}")
    return _providers[provider_type]


__all__ = [
    "JobProvider",
    "ProviderType",
    "GitHubJobProvider",
    "initialize_providers",
    "get_provider",
]
