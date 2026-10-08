"""Versioned, allowlisted preferences extracted from one conversation turn."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

INTENT_VERSION = "intent.v2"
PROMPT_VERSION = "intent-extraction.v2"
GameMode = Literal["purchase", "rental"]
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
    mode: GameMode | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "genre": self.genre,
            "style": self.style,
            "players": self.players,
            "price_min_brl_cents": self.price_min_brl_cents,
            "price_max_brl_cents": self.price_max_brl_cents,
            "constraints": list(self.constraints),
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, value: object) -> Intent | None:
        if not isinstance(value, dict):
            return None
        try:
            payload_value = dict(value)
            if isinstance(payload_value.get("constraints"), tuple):
                payload_value["constraints"] = list(payload_value["constraints"])
            # Checkpoints written before intent.v2 do not contain a mode.
            payload_value.setdefault("mode", None)
            payload_value.setdefault("mode_cleared", False)
            payload = IntentPayload.from_mapping(payload_value)
        except (TypeError, ValueError):
            return None
        return cls(
            platform=payload.platform,
            genre=payload.genre,
            style=payload.style,
            players=payload.players,
            price_min_brl_cents=payload.price_min_brl_cents,
            price_max_brl_cents=payload.price_max_brl_cents,
            constraints=tuple(payload.constraints),
            mode=payload.mode,
        )


@dataclass(frozen=True)
class IntentPayload:
    """Strict provider output for the six fields allowed by this story."""

    platform: str | None
    genre: str | None
    style: str | None
    players: int | None
    price_min_brl_cents: int | None
    price_max_brl_cents: int | None
    constraints: tuple[str, ...]
    mode: GameMode | None
    mode_cleared: bool

    @classmethod
    def from_mapping(cls, value: object) -> IntentPayload:
        if type(value) is not dict:
            raise ValueError("invalid_intent_object")
        values = cast(dict[str, object], value)
        fields = {
            "platform",
            "genre",
            "style",
            "players",
            "price_min_brl_cents",
            "price_max_brl_cents",
            "constraints",
            "mode",
            "mode_cleared",
        }
        if set(values) != fields:
            raise ValueError("invalid_intent_fields")

        platform = _optional_text(values["platform"], max_length=48)
        genre = _optional_text(values["genre"], max_length=64)
        style = _optional_text(values["style"], max_length=96)
        players = _optional_integer(values["players"], minimum=1, maximum=12)
        minimum = _optional_integer(
            values["price_min_brl_cents"], minimum=0, maximum=100_000_000
        )
        maximum = _optional_integer(
            values["price_max_brl_cents"], minimum=0, maximum=100_000_000
        )
        constraints = _constraints(values["constraints"])
        mode_value = values["mode"]
        if mode_value not in (None, "purchase", "rental"):
            raise ValueError("invalid_intent_mode")
        mode = cast(GameMode | None, mode_value)
        mode_cleared = values["mode_cleared"]
        if type(mode_cleared) is not bool or (mode_cleared and mode is not None):
            raise ValueError("invalid_intent_mode_clear")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("invalid_price_range")
        return cls(platform, genre, style, players, minimum, maximum, constraints, mode, mode_cleared)


@dataclass(frozen=True)
class IntentExtractionPayload(IntentPayload):
    clarification_field: ClarificationField

    @classmethod
    def from_json(cls, value: str) -> IntentExtractionPayload:
        payload = json.loads(value)
        if type(payload) is not dict:
            raise ValueError("invalid_intent_extraction_fields")
        payload_values = cast(dict[str, object], payload)
        if set(payload_values) != {
            "platform",
            "genre",
            "style",
            "players",
            "price_min_brl_cents",
            "price_max_brl_cents",
            "constraints",
            "mode",
            "mode_cleared",
            "clarification_field",
        }:
            raise ValueError("invalid_intent_extraction_fields")
        clarification = payload_values["clarification_field"]
        if clarification not in ("platform", "genre", "style", "players", "price_range", "none"):
            raise ValueError("invalid_clarification_field")
        clarification_field = cast(ClarificationField, clarification)
        intent = IntentPayload.from_mapping(
            {
                key: payload_values[key]
                for key in payload_values
                if key != "clarification_field"
            }
        )
        return cls(
            intent.platform,
            intent.genre,
            intent.style,
            intent.players,
            intent.price_min_brl_cents,
            intent.price_max_brl_cents,
            intent.constraints,
            intent.mode,
            intent.mode_cleared,
            clarification_field,
        )


def _optional_text(value: object, *, max_length: int) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > max_length:
        raise ValueError("invalid_intent_text")
    if not value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError("invalid_intent_text")
    return value


def _optional_integer(
    value: object, *, minimum: int, maximum: int
) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError("invalid_intent_integer")
    return value


def _constraints(value: object) -> tuple[str, ...]:
    if type(value) is not list or len(value) > 5:
        raise ValueError("invalid_intent_constraints")
    return tuple(_required_text(item, max_length=120) for item in value)


def _required_text(value: object, *, max_length: int) -> str:
    result = _optional_text(value, max_length=max_length)
    if result is None:
        raise ValueError("invalid_intent_text")
    return result


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
        mode=(None if extracted.mode_cleared else extracted.mode or previous.mode),
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
