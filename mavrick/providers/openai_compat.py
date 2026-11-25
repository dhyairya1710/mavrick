"""Chat Completions provider for OpenAI-compatible endpoints."""

from ..config import AITimeouts, PROVIDER_LABELS, ProviderSettings
from .base import Cancelled, ProviderError, ProviderHealth, ProviderResult, StreamCallback, structured_result
from .http_transport import JSONEndpoint, RequestBudget


class OpenAICompatibleProvider:
    def __init__(self, config: ProviderSettings, timeouts: AITimeouts):
        self.model = config.model
        self.label = PROVIDER_LABELS.get(config.name, "OpenAI-Compatible")
        self.timeout = timeouts.query_timeout
        self.health_timeout = min(timeouts.init_timeout, timeouts.query_timeout, 2)
        self.endpoint = JSONEndpoint(config.base_url, config.api_key)
        # A supplied API prefix is a complete root, including vendor routes such as /api/v1.
        self.api_root = self.endpoint.path or "/v1"

    def query(self, prompt: str, system_prompt: str, *, cancelled: Cancelled | None = None,
              stream_callback: StreamCallback | None = None) -> ProviderResult:
        path = self.api_root + "/chat/completions"
        budget = RequestBudget(self.timeout, cancelled)
        payload = {
            "model": self.model, "stream": False, "response_format": {"type": "json_object"}, "messages": [
                {"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}
            ],
        }
        try:
            result = self.endpoint.request(path, self.timeout, payload=payload, budget=budget,
                                           detect_unsupported_format=True)
        except ProviderError as error:
            if error.code != "unsupported_format":
                raise
            del payload["response_format"]
            result = self.endpoint.request(path, self.timeout, payload=payload, budget=budget)
        choices = result.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, dict) else None
        reply = structured_result(message.get("content") if isinstance(message, dict) else None)
        budget.interval()
        if stream_callback:
            stream_callback(reply.text)
        return reply

    def health_check(self) -> ProviderHealth:
        try:
            response = self.endpoint.request(self.api_root + "/models", self.health_timeout)
        except ProviderError as error:
            return ProviderHealth("offline", f"{self.label} unavailable", str(error))
        models = response.get("data")
        if not isinstance(models, list):
            return ProviderHealth("offline", f"{self.label} unavailable", "Provider returned an invalid model list.")
        if not any(isinstance(item, dict) and item.get("id") == self.model for item in models):
            return ProviderHealth("offline", f"{self.label} reachable", "Configured model is not listed.")
        return ProviderHealth("connected_provider", f"{self.label} reachable (model listed)")

    def close(self) -> None:
        """Each request closes its own connection."""
