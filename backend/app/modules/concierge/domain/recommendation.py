"""Versioned, sanitized evidence used for game recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from app.modules.concierge.domain.intent import Intent

RECOMMENDATION_VERSION = "recommendation.v1"
RANKING_PROMPT_VERSION = "recommendation-ranking.v1"


@dataclass(frozen=True)
class RankedGame:
    game_id: UUID
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class RankingPayload:
    recommendations: tuple[RankedGame, ...]

    @classmethod
    def from_json(cls, value: str) -> RankingPayload:
        import json

        payload = json.loads(value)
        if type(payload) is not dict or set(payload) != {"recommendations"}:
            raise ValueError("invalid_recommendation_ranking")
        rows = payload["recommendations"]
        if type(rows) is not list or len(rows) > 3:
            raise ValueError("invalid_recommendation_count")
        recommendations: list[RankedGame] = []
        for row in rows:
            if type(row) is not dict or set(row) != {"game_id", "evidence"}:
                raise ValueError("invalid_recommendation_item")
            try:
                game_id = UUID(row["game_id"])
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("invalid_recommendation_id") from exc
            if str(game_id) != row["game_id"]:
                raise ValueError("invalid_recommendation_id")
            evidence = row["evidence"]
            if (
                type(evidence) is not list
                or not 1 <= len(evidence) <= 8
                or any(type(item) is not str or len(item) > 96 for item in evidence)
                or len(set(evidence)) != len(evidence)
            ):
                raise ValueError("invalid_recommendation_evidence")
            recommendations.append(RankedGame(game_id, tuple(evidence)))
        if len({item.game_id for item in recommendations}) != len(recommendations):
            raise ValueError("duplicate_recommendation_id")
        return cls(tuple(recommendations))


@dataclass(frozen=True)
class RecommendationProvenance:
    recommendation_version: str
    ranking_prompt_version: str
    intent_version: str
    workflow_version: str
    configuration_version: str
    model_snapshot: str
    source_update_id: int
    correlation_id: UUID

    def to_dict(self) -> dict[str, object]:
        return {
            "recommendation_version": self.recommendation_version,
            "ranking_prompt_version": self.ranking_prompt_version,
            "intent_version": self.intent_version,
            "workflow_version": self.workflow_version,
            "configuration_version": self.configuration_version,
            "model_snapshot": self.model_snapshot,
            "source_update_id": self.source_update_id,
            "correlation_id": str(self.correlation_id),
        }


@dataclass(frozen=True)
class RecommendationOfferSnapshot:
    offer_id: UUID
    mode: Literal["purchase", "rental"]
    price_minor: int
    currency: Literal["BRL"]
    condition_summary: str
    available_units: int
    sandbox: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "offer_id": str(self.offer_id),
            "mode": self.mode,
            "price_minor": self.price_minor,
            "currency": self.currency,
            "condition_summary": self.condition_summary,
            "available_units": self.available_units,
            "sandbox": self.sandbox,
        }


@dataclass(frozen=True)
class RecommendationCandidateSnapshot:
    game_id: UUID
    title: str
    platform: str
    genre: str | None
    version: int
    offers: tuple[RecommendationOfferSnapshot, ...]
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "game_id": str(self.game_id),
            "title": self.title,
            "platform": self.platform,
            "genre": self.genre,
            "version": self.version,
            "offers": [offer.to_dict() for offer in self.offers],
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class RecommendationPlan:
    provenance: RecommendationProvenance
    intent: Intent
    candidates: tuple[RecommendationCandidateSnapshot, ...]
    ranking: tuple[RankedGame, ...]
    revalidation: dict[str, object] | None = None
    greeting_required: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "version": RECOMMENDATION_VERSION,
            "provenance": self.provenance.to_dict(),
            "intent": self.intent.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "ranking": [
                {
                    "game_id": str(item.game_id),
                    "evidence": list(item.evidence),
                }
                for item in self.ranking
            ],
            "revalidation": self.revalidation,
            "greeting_required": self.greeting_required,
        }

    @classmethod
    def from_dict(cls, value: object) -> RecommendationPlan | None:
        """Parse only the application-owned JSON shape persisted in the outbox."""
        if type(value) is not dict or set(value) != {
            "version", "provenance", "intent", "candidates", "ranking", "revalidation",
            "greeting_required",
        }:
            return None
        raw = cast(dict[str, object], value)
        if raw["version"] != RECOMMENDATION_VERSION:
            return None
        intent = Intent.from_dict(raw["intent"])
        provenance_value = raw["provenance"]
        if intent is None or type(provenance_value) is not dict:
            return None
        provenance_raw = cast(dict[str, object], provenance_value)
        if set(provenance_raw) != {
            "recommendation_version", "ranking_prompt_version", "intent_version",
            "workflow_version", "configuration_version", "model_snapshot",
            "source_update_id", "correlation_id",
        }:
            return None
        try:
            provenance = RecommendationProvenance(
                recommendation_version=_required_string(provenance_raw, "recommendation_version", 64),
                ranking_prompt_version=_required_string(provenance_raw, "ranking_prompt_version", 64),
                intent_version=_required_string(provenance_raw, "intent_version", 32),
                workflow_version=_required_string(provenance_raw, "workflow_version", 64),
                configuration_version=_required_string(provenance_raw, "configuration_version", 64),
                model_snapshot=_required_string(provenance_raw, "model_snapshot", 128),
                source_update_id=_required_integer(provenance_raw, "source_update_id", 0),
                correlation_id=UUID(_required_string(provenance_raw, "correlation_id", 36)),
            )
            candidates_raw = raw["candidates"]
            ranking_raw = raw["ranking"]
            if type(candidates_raw) is not list or not 1 <= len(candidates_raw) <= 20:
                return None
            if type(ranking_raw) is not list or not 1 <= len(ranking_raw) <= 3:
                return None
            candidates = tuple(_candidate_from_dict(item) for item in candidates_raw)
            ranking = tuple(_ranked_game_from_dict(item) for item in ranking_raw)
            if len({item.game_id for item in candidates}) != len(candidates):
                return None
            candidate_by_id = {item.game_id: item for item in candidates}
            if len({item.game_id for item in ranking}) != len(ranking):
                return None
            for item in ranking:
                candidate = candidate_by_id.get(item.game_id)
                if candidate is None or not set(item.evidence).issubset(candidate.evidence_refs):
                    return None
            revalidation = raw["revalidation"]
            if revalidation is not None and not _valid_revalidation(revalidation):
                return None
            greeting_required = raw["greeting_required"]
            if type(greeting_required) is not bool:
                return None
            return cls(
                provenance,
                intent,
                candidates,
                ranking,
                cast(dict[str, object] | None, revalidation),
                greeting_required,
            )
        except (KeyError, ValueError, TypeError):
            return None


def _required_string(values: dict[str, object], key: str, maximum: int) -> str:
    value = values[key]
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError("invalid_recommendation_context")
    return value


def _required_integer(values: dict[str, object], key: str, minimum: int) -> int:
    value = values[key]
    if type(value) is not int or value < minimum:
        raise ValueError("invalid_recommendation_context")
    return value


def _candidate_from_dict(value: object) -> RecommendationCandidateSnapshot:
    if type(value) is not dict or set(value) != {
        "game_id", "title", "platform", "genre", "version", "offers", "evidence_refs"
    }:
        raise ValueError("invalid_recommendation_candidate")
    raw = cast(dict[str, object], value)
    game_id = UUID(_required_string(raw, "game_id", 36))
    title = _required_string(raw, "title", 200)
    platform = _required_string(raw, "platform", 96)
    genre_value = raw["genre"]
    if genre_value is not None and (type(genre_value) is not str or len(genre_value) > 96):
        raise ValueError("invalid_recommendation_candidate")
    version = _required_integer(raw, "version", 1)
    offers_raw = raw["offers"]
    evidence_raw = raw["evidence_refs"]
    if type(offers_raw) is not list or not offers_raw or len(offers_raw) > 20:
        raise ValueError("invalid_recommendation_candidate")
    if type(evidence_raw) is not list or not 1 <= len(evidence_raw) <= 20:
        raise ValueError("invalid_recommendation_candidate")
    if any(type(item) is not str or len(item) > 96 for item in evidence_raw):
        raise ValueError("invalid_recommendation_candidate")
    offers = tuple(_offer_from_dict(item) for item in offers_raw)
    return RecommendationCandidateSnapshot(
        game_id, title, platform, cast(str | None, genre_value), version, offers,
        tuple(evidence_raw),
    )


def _offer_from_dict(value: object) -> RecommendationOfferSnapshot:
    if type(value) is not dict or set(value) != {
        "offer_id", "mode", "price_minor", "currency", "condition_summary",
        "available_units", "sandbox",
    }:
        raise ValueError("invalid_recommendation_offer")
    raw = cast(dict[str, object], value)
    offer_id = UUID(_required_string(raw, "offer_id", 36))
    mode = raw["mode"]
    currency = raw["currency"]
    sandbox = raw["sandbox"]
    if mode not in {"purchase", "rental"} or currency != "BRL" or type(sandbox) is not bool:
        raise ValueError("invalid_recommendation_offer")
    price = _required_integer(raw, "price_minor", 0)
    units = _required_integer(raw, "available_units", 0)
    condition_summary = raw["condition_summary"]
    if type(condition_summary) is not str:
        raise ValueError("invalid_recommendation_offer")
    return RecommendationOfferSnapshot(
        offer_id, cast(Literal["purchase", "rental"], mode), price,
        cast(Literal["BRL"], currency), condition_summary,
        units, sandbox,
    )


def _ranked_game_from_dict(value: object) -> RankedGame:
    if type(value) is not dict or set(value) != {"game_id", "evidence"}:
        raise ValueError("invalid_recommendation_ranking")
    raw = cast(dict[str, object], value)
    game_id = UUID(_required_string(raw, "game_id", 36))
    evidence = raw["evidence"]
    if type(evidence) is not list or not 1 <= len(evidence) <= 8:
        raise ValueError("invalid_recommendation_ranking")
    if any(type(item) is not str or len(item) > 96 for item in evidence):
        raise ValueError("invalid_recommendation_ranking")
    return RankedGame(game_id, tuple(evidence))


def _valid_revalidation(value: object) -> bool:
    if type(value) is not dict or set(value) != {"checked_at", "facts"}:
        return False
    raw = cast(dict[str, object], value)
    checked_at = raw["checked_at"]
    facts = raw["facts"]
    if type(checked_at) is not str or len(checked_at) > 40:
        return False
    try:
        from datetime import datetime

        datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if type(facts) is not list or not 1 <= len(facts) <= 3:
        return False
    seen: set[UUID] = set()
    for fact in facts:
        if type(fact) is not dict:
            return False
        status = fact.get("status")
        try:
            game_id = UUID(_required_string(cast(dict[str, object], fact), "game_id", 36))
        except (KeyError, ValueError, TypeError):
            return False
        if game_id in seen or status not in {
            "eligible", "unpublished", "catalog_changed", "no_eligible_offer",
            "revalidation_unavailable",
        }:
            return False
        seen.add(game_id)
        if status == "eligible":
            if set(fact) != {
                "game_id", "status", "title", "platform", "genre", "version", "offers"
            }:
                return False
            try:
                _required_string(cast(dict[str, object], fact), "title", 200)
                _required_string(cast(dict[str, object], fact), "platform", 96)
                genre = fact["genre"]
                if genre is not None and (type(genre) is not str or len(genre) > 96):
                    return False
                _required_integer(cast(dict[str, object], fact), "version", 1)
                offers = fact["offers"]
                if type(offers) is not list or not offers:
                    return False
                for offer in offers:
                    _offer_from_dict(offer)
            except (KeyError, ValueError, TypeError):
                return False
        elif set(fact) != {"game_id", "status"}:
            return False
    return True
