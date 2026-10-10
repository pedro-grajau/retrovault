"""Consent and notification values independent of channels and persistence."""

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.modules.concierge.domain.intent import redact_sensitive_text

DemandStatus = Literal["proposed", "active", "cancelled", "notified", "sold"]
DEMAND_VERSION = "demand.v1"
LIMITS = "Não há reserva, prioridade, garantia de aquisição ou prazo. O interesse termina após o primeiro aviso entregue; outro aviso exige novo consentimento."


def clean_description(value: str, maximum: int = 200) -> str:
    return " ".join(redact_sensitive_text(value).split())[:maximum]


def normalize(value: str) -> str:
    return unicodedata.normalize(
        "NFKC", clean_description(value, maximum=len(value))
    ).casefold()


@dataclass(frozen=True)
class Demand:
    id: UUID
    owner_key: str
    channel: str
    title: str
    platform: str
    mode: str
    game_id: UUID | None
    status: DemandStatus
    created_at: datetime
    consented_at: datetime | None = None


@dataclass(frozen=True)
class DemandNotification:
    id: UUID
    demand: Demand
    chat_id: str
    event_id: UUID
    lease_token: UUID
    attempts: int


class DemandsUnavailable(RuntimeError):
    pass
