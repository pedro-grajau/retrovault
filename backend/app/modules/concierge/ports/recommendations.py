"""Application port for budgeted, evidence-backed recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from app.modules.concierge.domain.intent import Intent


@dataclass(frozen=True)
class RecommendationOutcome:
    status: Literal["accepted", "empty", "fallback"]
    reply_text: str
    context: dict[str, object] | None = None


class Recommendations(Protocol):
    def recommend(
        self,
        intent: Intent,
        *,
        session_id: UUID,
        channel: str,
        update_id: int,
        correlation_id: UUID,
        excluded_game_ids: tuple[UUID, ...] = (),
        previous_recommendation: object = None,
    ) -> RecommendationOutcome: ...

    def revalidate_and_compose(self, context: object) -> str: ...
