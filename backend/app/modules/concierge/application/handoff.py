"""Sandbox handoff policy with a single, explicitly configured recipient."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from app.modules.concierge.domain.handoff import HandoffUnavailable
from app.modules.concierge.domain.intent import Intent, redact_sensitive_text
from app.modules.concierge.ports.handoffs import HandoffStore

HandoffStatus = Literal["registered", "disabled", "unavailable"]


@dataclass(frozen=True)
class HandoffDecision:
    status: HandoffStatus


class HandoffService:
    def __init__(
        self,
        store: HandoffStore | None,
        allowed_user_ids: frozenset[int],
        *,
        telegram_token_configured: bool = True,
    ) -> None:
        self.store = store
        self.allowed_user_ids = allowed_user_ids
        self.telegram_token_configured = telegram_token_configured

    def request(
        self,
        *,
        session_id: UUID,
        channel: str,
        update_id: int,
        correlation_id: UUID,
        context_snapshot: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> HandoffDecision:
        if (
            len(self.allowed_user_ids) != 1
            or self.store is None
            or not self.telegram_token_configured
            or next(iter(self.allowed_user_ids)) <= 0
        ):
            return HandoffDecision("disabled")
        recipient_id = next(iter(self.allowed_user_ids))
        snapshot: dict[str, object] | None = None
        if context_snapshot is None:
            notification = (
                "Sandbox: solicitação de revisão humana registrada. "
                f"Correlação: {correlation_id}. Nenhum histórico de conversa foi encaminhado."
            )
        else:
            snapshot = _validated_context(context_snapshot, correlation_id)
            notification = _context_notification(snapshot, correlation_id)
        try:
            created_or_existing = self.store.request_handoff(
                session_id=session_id,
                channel=channel,
                update_id=update_id,
                correlation_id=correlation_id,
                target_chat_id=str(recipient_id),
                notification_text=notification,
                context_snapshot=snapshot,
                now=now or datetime.now(UTC),
            )
        except HandoffUnavailable:
            return HandoffDecision("unavailable")
        return HandoffDecision("registered" if created_or_existing else "unavailable")


def _validated_context(
    value: dict[str, object], correlation_id: UUID
) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "version", "correlation_id", "intent", "options_considered", "rejected_options"
    }:
        raise ValueError("invalid_handoff_context")
    if value["version"] != "handoff-context.v1" or value["correlation_id"] != str(correlation_id):
        raise ValueError("invalid_handoff_context")
    intent = Intent.from_dict(value["intent"])
    if intent is None:
        raise ValueError("invalid_handoff_context_intent")
    considered = _validated_options(value["options_considered"], rejected=False)
    rejected = _validated_options(value["rejected_options"], rejected=True)
    return {
        "version": "handoff-context.v1",
        "correlation_id": str(correlation_id),
        "intent": intent.to_dict(),
        "options_considered": considered,
        "rejected_options": rejected,
    }


def _validated_options(value: object, *, rejected: bool) -> list[dict[str, object]]:
    if type(value) is not list or len(value) > 100:
        raise ValueError("invalid_handoff_context_options")
    result: list[dict[str, object]] = []
    seen: set[UUID] = set()
    for item in value:
        expected = (
            {
                "game_id", "title", "reason", "source_update_id",
                "recommendation_update_id",
            }
            if rejected
            else {"game_id", "title"}
        )
        if type(item) is not dict or set(item) != expected:
            raise ValueError("invalid_handoff_context_option")
        raw = cast(dict[str, object], item)
        raw_game_id = raw["game_id"]
        if not isinstance(raw_game_id, str):
            raise ValueError("invalid_handoff_context_option_id")
        try:
            game_id = UUID(raw_game_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid_handoff_context_option_id") from exc
        title = raw["title"]
        if type(title) is not str:
            raise ValueError("invalid_handoff_context_option")
        title = redact_sensitive_text(title)
        if (
            str(game_id) != raw["game_id"]
            or (not rejected and game_id in seen)
            or not title
            or len(title) > 200
            or any(not character.isprintable() for character in title)
        ):
            raise ValueError("invalid_handoff_context_option")
        seen.add(game_id)
        normalized: dict[str, object] = {"game_id": str(game_id), "title": title}
        if rejected:
            reason = raw["reason"]
            update_id = raw["source_update_id"]
            recommendation_update_id = raw["recommendation_update_id"]
            if (
                reason not in {
                    "price", "platform", "genre", "style", "condition",
                    "availability", "players", "other",
                }
                or type(update_id) is not int
                or update_id < 0
                or type(recommendation_update_id) is not int
                or recommendation_update_id < 0
            ):
                raise ValueError("invalid_handoff_context_rejection")
            normalized.update(
                {
                    "reason": reason,
                    "source_update_id": update_id,
                    "recommendation_update_id": recommendation_update_id,
                }
            )
        result.append(normalized)
    return result


def _context_notification(snapshot: dict[str, object], correlation_id: UUID) -> str:
    intent = Intent.from_dict(snapshot["intent"]) or Intent()
    preference_parts = [
        f"plataforma={intent.platform}" if intent.platform else "",
        f"gênero={intent.genre}" if intent.genre else "",
        f"estilo={intent.style}" if intent.style else "",
        _price_summary(intent.price_min_brl_cents, intent.price_max_brl_cents),
        f"modalidade={intent.mode}" if intent.mode else "",
        f"restrições={'; '.join(intent.constraints)}" if intent.constraints else "",
    ]
    considered = cast(list[dict[str, object]], snapshot["options_considered"])
    rejected = cast(list[dict[str, object]], snapshot["rejected_options"])
    prioritized: list[str] = []
    if rejected:
        item = rejected[-1]
        prioritized.append(
            f"recusada {item['title']} ({item['game_id']}): {item['reason']}"
        )
    prioritized.extend(part for part in preference_parts if part)
    prioritized.extend(
        f"considerada {item['title']} ({item['game_id']})"
        for item in considered[-3:]
    )
    prefix = "Sandbox: revisão humana; "
    suffix = f". Correlação: {correlation_id}. Sem histórico de conversa."
    body = "; ".join(prioritized)
    return prefix + body[: max(0, 512 - len(prefix) - len(suffix))] + suffix


def _price_summary(minimum: int | None, maximum: int | None) -> str:
    if minimum is None and maximum is None:
        return ""
    if minimum is not None and maximum is not None:
        return f"preço={_format_brl(minimum)} a {_format_brl(maximum)}"
    if minimum is not None:
        return f"preço=a partir de {_format_brl(minimum)}"
    return f"preço=até {_format_brl(maximum or 0)}"


def _format_brl(cents: int) -> str:
    reais, centavos = divmod(cents, 100)
    return f"R$ {reais:,}".replace(",", ".") + f",{centavos:02d}"
