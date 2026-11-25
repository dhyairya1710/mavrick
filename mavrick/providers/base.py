"""Transport-independent AI provider contracts."""

import json
import re
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

Cancelled = Callable[[], bool]
StreamCallback = Callable[[str], None]
ErrorCode = Literal["cancelled", "timeout", "transport", "http", "invalid_response", "configuration",
                    "unsupported_format"]


class ProviderError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProviderResult:
    text: str


@dataclass(frozen=True)
class ProviderHealth:
    status: str
    display: str
    error: str = ""
    rss_mb: float = 0
    socket: str = ""
    binary: str = ""


class AIProvider(Protocol):
    def query(self, prompt: str, system_prompt: str, *, cancelled: Cancelled | None = None,
              stream_callback: StreamCallback | None = None) -> ProviderResult: ...

    def health_check(self) -> ProviderHealth: ...

    def close(self) -> None: ...


def unwrap_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    return text


def structured_result(text: object) -> ProviderResult:
    if not isinstance(text, str):
        raise ProviderError("invalid_response", "Provider response has no text content.")
    normalized = unwrap_json(text)
    try:
        value = json.loads(normalized)
    except (ValueError, RecursionError):
        raise ProviderError("invalid_response", "Provider reply was not valid JSON.") from None
    if not isinstance(value, dict):
        raise ProviderError("invalid_response", "Provider reply must be a JSON object.")
    return ProviderResult(normalized)
