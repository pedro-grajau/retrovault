"""Versioned, allowlisted preferences extracted from one conversation turn."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

INTENT_VERSION = "intent.v1"
PROMPT_VERSION = "intent-extraction.v1"
IntentField = Literal["platform", "genre", "style", "players", "price_range"]
ClarificationField = IntentField | Literal["none"]

_INJECTION_MARKERS = re.compile(
    r"(?:ignore|disregard|desconsidere|esqueça).{0,80}"
    r"(?:instructions?|rules?|instruções|regras|prompt|system|developer)|"
    r"(?:reveal|mostre|revele).{0,80}(?:prompt|instructions?|instruções|segredo)|"
    r"(?:jailbreak|bypass|override|modo desenvolvedor)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class Intent:
    platform: str | None = None
    genre: str | None = None
    style: str | None = None
    players: int | None = None
    price_min_brl_cents: int | None = None
    price_max_brl_cents: int | None = None
    constraints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "genre": self.genre,
            "style": self.style,
            "players": self.players,
            "price_min_brl_cents": self.price_min_brl_cents,
            "price_max_brl_cents": self.price_max_brl_cents,
            "constraints": list(self.constraints),
        }

    @classmethod
    def from_dict(cls, value: object) -> Intent | None:
        if not isinstance(value, dict):
            return None
        try:
            payload_value = dict(value)
            if isinstance(payload_value.get("constraints"), tuple):
                payload_value["constraints"] = list(payload_value["constraints"])
            payload = IntentPayload.model_validate(payload_value)
        except ValidationError:
            return None
        return cls(
            platform=payload.platform,
            genre=payload.genre,
            style=payload.style,
            players=payload.players,
            price_min_brl_cents=payload.price_min_brl_cents,
            price_max_brl_cents=payload.price_max_brl_cents,
            constraints=tuple(payload.constraints),
        )


class IntentPayload(BaseModel):
    """Strict provider output for the six fields allowed by this story."""

    model_config = ConfigDict(extra="forbid", strict=True)

    platform: str | None = Field(max_length=48)
    genre: str | None = Field(max_length=64)
    style: str | None = Field(max_length=96)
    players: int | None = Field(ge=1, le=12)
    price_min_brl_cents: int | None = Field(ge=0, le=100_000_000)
    price_max_brl_cents: int | None = Field(ge=0, le=100_000_000)
    constraints: list[Annotated[str, Field(max_length=120)]] = Field(max_length=5)

    @model_validator(mode="after")
    def validate_price_range(self) -> IntentPayload:
        if (
            self.price_min_brl_cents is not None
            and self.price_max_brl_cents is not None
            and self.price_min_brl_cents > self.price_max_brl_cents
        ):
            raise ValueError("invalid_price_range")
        for value in (self.platform, self.genre, self.style, *self.constraints):
            if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
                raise ValueError("invalid_intent_text")
        return self


class IntentExtractionPayload(IntentPayload):
    model_config = ConfigDict(extra="forbid", strict=True)

    clarification_field: ClarificationField


@dataclass(frozen=True)
class IntentProvenance:
    intent_version: str
    prompt_version: str
    workflow_version: str
    configuration_version: str
    source_update_id: int
    correlation_id: UUID
    safety_classification: Literal["normal", "suspected_injection"]

    def to_dict(self) -> dict[str, object]:
        return {
            "intent_version": self.intent_version,
            "prompt_version": self.prompt_version,
            "workflow_version": self.workflow_version,
            "configuration_version": self.configuration_version,
            "source_update_id": self.source_update_id,
            "correlation_id": str(self.correlation_id),
            "safety_classification": self.safety_classification,
        }


def merge_intent(previous: Intent | None, extracted: IntentPayload) -> Intent:
    """Apply only non-null preferences and retain earlier explicit preferences."""

    previous = previous or Intent()
    constraints = list(extracted.constraints)
    for item in previous.constraints:
        if item not in constraints:
            constraints.append(item)
    minimum = (
        extracted.price_min_brl_cents
        if extracted.price_min_brl_cents is not None
        else previous.price_min_brl_cents
    )
    maximum = (
        extracted.price_max_brl_cents
        if extracted.price_max_brl_cents is not None
        else previous.price_max_brl_cents
    )
    if (
        extracted.price_min_brl_cents is not None
        and extracted.price_max_brl_cents is None
        and maximum is not None
        and extracted.price_min_brl_cents > maximum
    ):
        maximum = None
    if (
        extracted.price_max_brl_cents is not None
        and extracted.price_min_brl_cents is None
        and minimum is not None
        and extracted.price_max_brl_cents < minimum
    ):
        minimum = None
    return Intent(
        platform=extracted.platform or previous.platform,
        genre=extracted.genre or previous.genre,
        style=extracted.style or previous.style,
        players=extracted.players or previous.players,
        price_min_brl_cents=minimum,
        price_max_brl_cents=maximum,
        constraints=tuple(constraints[:5]),
    )


def is_prompt_injection(text: str) -> bool:
    return _INJECTION_MARKERS.search(text) is not None


def clarification_question(field: ClarificationField, intent: Intent) -> str | None:
    """Render one fixed question, and only when its material field is absent."""

    missing = {
        "platform": intent.platform is None,
        "genre": intent.genre is None,
        "style": intent.style is None,
        "players": intent.players is None,
        "price_range": (
            intent.price_min_brl_cents is None
            and intent.price_max_brl_cents is None
        ),
    }
    if field == "none" or not missing[field]:
        return None
    return {
        "platform": "Em qual plataforma você quer jogar?",
        "genre": "Qual gênero você está procurando?",
        "style": "Que tipo de experiência você prefere?",
        "players": "Quantas pessoas vão jogar?",
        "price_range": "Qual faixa de preço você tem em mente?",
    }[field]
