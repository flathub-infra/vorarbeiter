from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Dict


class ProviderType(Enum):
    GITHUB = "github"


class JobProvider(ABC):
    provider_type: ProviderType

    @abstractmethod
    async def initialize(self, config: Dict[str, Any]) -> None:
        pass

    @abstractmethod
    async def dispatch(
        self, job_id: str, pipeline_id: str, job_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def cancel(self, job_id: str, provider_data: Dict[str, Any]) -> bool:
        pass
