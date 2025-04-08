from typing import Dict, Any, Type

from app.providers.base import JobProvider, ProviderType
from app.providers.github import GitHubJobProvider


class ProviderFactory:
    _providers: Dict[ProviderType, Type[JobProvider]] = {
        ProviderType.GITHUB: GitHubJobProvider,
    }

    @classmethod
    async def create_provider(
        cls, provider_type: ProviderType, config: Dict[str, Any]
    ) -> JobProvider:
        provider_class = cls._providers.get(provider_type)
        if not provider_class:
            raise ValueError(f"Unsupported provider type: {provider_type}")

        provider = provider_class()
        await provider.initialize(config)
        return provider
