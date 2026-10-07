"""Provider-neutral model gateway contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.concierge.domain.intent import Intent


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ModelExtraction:
    output_json: str
    usage: ModelUsage


class ModelCallFailure(RuntimeError):
    def __init__(self, *, conclusive: bool) -> None:
        super().__init__("model_call_failed")
        self.conclusive = conclusive


class ModelGateway(Protocol):
    def input_token_upper_bound(
        self, message_text: str, previous_intent: Intent | None
    ) -> int: ...

    def extract_intent(
        self,
        message_text: str,
        previous_intent: Intent | None,
        *,
        correlation_id: UUID,
    ) -> ModelExtraction: ...
