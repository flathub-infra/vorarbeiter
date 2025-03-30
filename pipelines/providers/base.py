from abc import ABC, abstractmethod


class BaseProvider(ABC):
    def __init__(self, provider_model):
        self.provider_model = provider_model

    @abstractmethod
    async def dispatch_job(self, job_instance):
        pass

    @abstractmethod
    async def cancel_job(self, job_instance):
        pass
