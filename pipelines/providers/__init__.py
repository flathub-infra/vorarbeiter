from typing import Dict, Type
from .base import BaseProvider
from .github_actions import GitHubActionsProvider


class ProviderRegistry:
    _registry: Dict[str, Type[BaseProvider]] = {}

    @classmethod
    def register(cls, provider_type: str, provider_class: Type[BaseProvider]) -> None:
        cls._registry[provider_type] = provider_class

    @classmethod
    def get_provider(cls, provider_type: str) -> Type[BaseProvider]:
        if provider_type not in cls._registry:
            raise ValueError(f"Provider type '{provider_type}' not registered")
        return cls._registry[provider_type]


ProviderRegistry.register("github", GitHubActionsProvider)
