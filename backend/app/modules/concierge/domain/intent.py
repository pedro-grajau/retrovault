"""Versioned, allowlisted preferences extracted from one conversation turn."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

INTENT_VERSION = "intent.v2"
PROMPT_VERSION = "intent-extraction.v3"
REFINEMENT_VERSION = "refinement.v1"
GameMode = Literal["purchase", "rental"]
IntentField = Literal["platform", "genre", "style", "players", "price_range"]
ClearableIntentField = Literal[
    "platform", "genre", "style", "players", "price_range", "constraints"
]
ClarificationField = IntentField | Literal["none"]
RejectionReason = Literal[
    "price", "platform", "genre", "style", "condition", "availability",
    "players", "other",
]

_INJECTION_MARKERS = re.compile(
    r"(?:ignore|disregard|desconsidere|esqueça).{0,80}"
    r"(?:instructions?|rules?|instruções|regras|prompt|system|developer)|"
    r"(?:reveal|mostre|revele).{0,80}(?:prompt|instructions?|instruções|segredo)|"
    r"(?:jailbreak|bypass|override|modo desenvolvedor)",
    re.IGNORECASE | re.DOTALL,
)
_SENSITIVE_TEXT = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"(?<!\w)(?:\+?\d[\d().\- ]{7,}\d)(?!\w)",
    re.IGNORECASE,
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
            # Clearing signals are transient extraction metadata, never persisted.
            payload_value["cleared_fields"] = []
            for field in ("platform", "genre", "style"):
                field_value = payload_value.get(field)
                if (
                    isinstance(field_value, str)
                    and _SENSITIVE_TEXT.search(field_value) is not None
                ):
                    payload_value[field] = None
            raw_constraints = payload_value.get("constraints")
            if isinstance(raw_constraints, (list, tuple)):
                payload_value["constraints"] = [
                    item
                    for item in raw_constraints
                    if not (
                        isinstance(item, str)
                        and _SENSITIVE_TEXT.search(item) is not None
                    )
                ]
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
    """Strict provider output, including transient preference-clearing signals."""

    platform: str | None
    genre: str | None
    style: str | None
    players: int | None
    price_min_brl_cents: int | None
    price_max_brl_cents: int | None
    constraints: tuple[str, ...]
    mode: GameMode | None
    mode_cleared: bool
    cleared_fields: tuple[ClearableIntentField, ...]

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
            "cleared_fields",
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
        raw_cleared_fields = values["cleared_fields"]
        allowed_cleared_fields = {
            "platform",
            "genre",
            "style",
            "players",
            "price_range",
            "constraints",
        }
        if (
            type(raw_cleared_fields) is not list
            or len(raw_cleared_fields) > len(allowed_cleared_fields)
            or any(
                type(item) is not str or item not in allowed_cleared_fields
                for item in raw_cleared_fields
            )
            or len(set(raw_cleared_fields)) != len(raw_cleared_fields)
        ):
            raise ValueError("invalid_cleared_fields")
        cleared_fields = cast(
            tuple[ClearableIntentField, ...], tuple(raw_cleared_fields)
        )
        for field, field_value in (
            ("platform", platform),
            ("genre", genre),
            ("style", style),
            ("players", players),
        ):
            if field in cleared_fields and field_value is not None:
                raise ValueError("conflicting_cleared_field_value")
        if "price_range" in cleared_fields and (
            minimum is not None or maximum is not None
        ):
            raise ValueError("conflicting_cleared_price_range")
        if "constraints" in cleared_fields and constraints:
            raise ValueError("conflicting_cleared_constraints")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("invalid_price_range")
        return cls(
            platform,
            genre,
            style,
            players,
            minimum,
            maximum,
            constraints,
            mode,
            mode_cleared,
            cleared_fields,
        )


@dataclass(frozen=True)
class IntentExtractionPayload(IntentPayload):
    clarification_field: ClarificationField
    rejections: tuple[IntentRejection, ...]
    rejection_ambiguous: bool

    @classmethod
    def from_json(cls, value: str) -> IntentExtractionPayload:
        payload = json.loads(value)
        if type(payload) is not dict:
            raise ValueError("invalid_intent_extraction_fields")
        payload_values = cast(dict[str, object], payload)
        legacy_fields = {
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
        }
        current_fields = legacy_fields | {"rejections", "rejection_ambiguous"}
        latest_fields = current_fields | {"cleared_fields"}
        if frozenset(payload_values) not in {
            frozenset(legacy_fields),
            frozenset(current_fields),
            frozenset(latest_fields),
        }:
            raise ValueError("invalid_intent_extraction_fields")
        clarification = payload_values["clarification_field"]
        if clarification not in ("platform", "genre", "style", "players", "price_range", "none"):
            raise ValueError("invalid_clarification_field")
        clarification_field = cast(ClarificationField, clarification)
        rejection_ambiguous = payload_values.get("rejection_ambiguous", False)
        if type(rejection_ambiguous) is not bool:
            raise ValueError("invalid_rejection_ambiguity")
        raw_rejections = payload_values.get("rejections", [])
        if type(raw_rejections) is not list or len(raw_rejections) > 3:
            raise ValueError("invalid_rejections")
        rejections: list[IntentRejection] = []
        for item in raw_rejections:
            if type(item) is not dict or set(item) != {"game_id", "reason"}:
                raise ValueError("invalid_rejection")
            item = cast(dict[str, object], item)
            raw_game_id = item["game_id"]
            if not isinstance(raw_game_id, str):
                raise ValueError("invalid_rejection_id")
            try:
                game_id = UUID(raw_game_id)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("invalid_rejection_id") from exc
            if str(game_id) != item["game_id"]:
                raise ValueError("invalid_rejection_id")
            reason = item["reason"]
            if reason not in {
                "price", "platform", "genre", "style", "condition",
                "availability", "players", "other",
            }:
                raise ValueError("invalid_rejection_reason")
            rejections.append(IntentRejection(game_id, cast(RejectionReason, reason)))
        if len({item.game_id for item in rejections}) != len(rejections):
            raise ValueError("duplicate_rejection_id")
        if rejection_ambiguous and rejections:
            raise ValueError("ambiguous_rejection_has_targets")
        intent_values = {
            key: payload_values[key]
            for key in payload_values
            if key not in {
                "clarification_field", "rejections", "rejection_ambiguous"
            }
        }
        # Accept the prior v3 response shape during a rolling provider rollout.
        intent_values.setdefault("cleared_fields", [])
        intent = IntentPayload.from_mapping(intent_values)
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
            intent.cleared_fields,
            clarification_field,
            tuple(rejections),
            rejection_ambiguous,
        )


@dataclass(frozen=True)
class IntentRejection:
    """A normalized rejection tied to one option from the preceding round."""

    game_id: UUID
    reason: RejectionReason

    def to_dict(self) -> dict[str, str]:
        return {"game_id": str(self.game_id), "reason": self.reason}


def _optional_text(value: object, *, max_length: int) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > max_length:
        raise ValueError("invalid_intent_text")
    if (
        not value.strip()
        or any(ord(character) < 32 for character in value)
        or _SENSITIVE_TEXT.search(value) is not None
    ):
        raise ValueError("invalid_intent_text")
    return value


def redact_sensitive_text(value: str) -> str:
    """Remove email addresses and phone-like values from published text."""
    return _SENSITIVE_TEXT.sub("[redigido]", value)


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
    cleared = set(extracted.cleared_fields)
    constraints = list(extracted.constraints)
    if "constraints" not in cleared:
        for item in previous.constraints:
            if item not in constraints:
                constraints.append(item)
    minimum = None if "price_range" in cleared else (
        extracted.price_min_brl_cents
        if extracted.price_min_brl_cents is not None
        else previous.price_min_brl_cents
    )
    maximum = None if "price_range" in cleared else (
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
        platform=(
            None if "platform" in cleared else extracted.platform or previous.platform
        ),
        genre=(
            None if "genre" in cleared else extracted.genre or previous.genre
        ),
        style=(
            None if "style" in cleared else extracted.style or previous.style
        ),
        players=(
            None if "players" in cleared else extracted.players or previous.players
        ),
        price_min_brl_cents=minimum,
        price_max_brl_cents=maximum,
        constraints=tuple(constraints[:5]),
        mode=(None if extracted.mode_cleared else extracted.mode or previous.mode),
    )


def is_prompt_injection(text: str) -> bool:
    return _INJECTION_MARKERS.search(text) is not None


def clarification_question(
    field: ClarificationField, intent: Intent, *, force: bool = False
) -> str | None:
    """Render a fixed question when a field is absent or explicitly forced."""

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
    if field == "none" or (not force and not missing[field]):
        return None
    return {
        "platform": "Em qual plataforma você quer jogar?",
        "genre": "Qual gênero você está procurando?",
        "style": "Que tipo de experiência você prefere?",
        "players": "Quantas pessoas vão jogar?",
        "price_range": "Qual faixa de preço você tem em mente?",
    }[field]
