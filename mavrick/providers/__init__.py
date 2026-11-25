"""Construct the configured provider without making a network request."""

from ..config import AITimeouts, ProviderSettings, load_ai_timeouts
from .agy import AgyProvider
from .base import AIProvider, ProviderError
from .ollama import OllamaProvider
from .openai_compat import OpenAICompatibleProvider

PROVIDERS = {
    "agy": AgyProvider,
    "ollama": OllamaProvider,
    "openai_compat": OpenAICompatibleProvider,
    "groq": OpenAICompatibleProvider,
    "cerebras": OpenAICompatibleProvider,
    "openrouter": OpenAICompatibleProvider,
}


def create_provider(config: ProviderSettings, timeouts: AITimeouts | None = None, **agy_options) -> AIProvider:
    timing = timeouts or load_ai_timeouts()
    if config.name not in PROVIDERS:
        raise ProviderError("configuration", "Unknown AI provider.")
    if config.name == "agy":
        return AgyProvider(config.model, timing, **agy_options)
    return PROVIDERS[config.name](config, timing)
