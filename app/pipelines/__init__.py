from app.pipelines.build import BuildPipeline
from app.providers import initialize_providers

_providers_initialized = False


async def ensure_providers_initialized():
    global _providers_initialized
    if not _providers_initialized:
        await initialize_providers()
        _providers_initialized = True


__all__ = ["BuildPipeline", "ensure_providers_initialized"]
