"""Ollama chat provider using its native JSON response mode."""

from ..config import AITimeouts, ProviderSettings
from .base import Cancelled, ProviderError, ProviderHealth, ProviderResult, StreamCallback, structured_result
from .http_transport import JSONEndpoint


class OllamaProvider:
    def __init__(self, config: ProviderSettings, timeouts: AITimeouts):
        self.model = config.model
        self.timeout = timeouts.query_timeout
        self.health_timeout = min(timeouts.init_timeout, timeouts.query_timeout, 2)
        self.endpoint = JSONEndpoint(config.base_url, config.api_key)
        base = self.endpoint.path
        self.api_root = base if base.endswith("/api") else base + "/api"

    def query(self, prompt: str, system_prompt: str, *, cancelled: Cancelled | None = None,
              stream_callback: StreamCallback | None = None) -> ProviderResult:
        result = self.endpoint.request(self.api_root + "/chat", self.timeout, cancelled=cancelled, payload={
            "model": self.model, "stream": False, "format": "json", "messages": [
                {"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}
            ],
        })
        message = result.get("message")
        reply = structured_result(message.get("content") if isinstance(message, dict) else None)
        if stream_callback:
            stream_callback(reply.text)
        return reply

    def health_check(self) -> ProviderHealth:
        try:
            response = self.endpoint.request(self.api_root + "/tags", self.health_timeout)
        except ProviderError as error:
            return ProviderHealth("offline", "Ollama unavailable", str(error))
        models = response.get("models")
        if not isinstance(models, list):
            return ProviderHealth("offline", "Ollama unavailable", "Provider returned an invalid model list.")
        names = {item.get("model", item.get("name")) for item in models
                 if isinstance(item, dict) and isinstance(item.get("model", item.get("name")), str)}
        if self.model not in names and self.model + ":latest" not in names:
            return ProviderHealth("offline", "Ollama reachable", "Configured model is not listed locally.")
        return ProviderHealth("connected_provider", "Ollama reachable (model listed)")

    def close(self) -> None:
        """Each request closes its own connection."""
