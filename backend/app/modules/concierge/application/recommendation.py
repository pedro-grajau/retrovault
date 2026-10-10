"""Budgeted candidate ranking and deterministic fact-backed response composition."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from app.modules.catalog.application.discovery import (
    DiscoveryCandidate,
    PublicDiscovery,
    RecommendationCriteria,
)
from app.modules.catalog.domain.publication import PublishedGame
from app.modules.commerce.domain.offers import Offer
from app.modules.concierge.domain.ai_ledger import (
    AiLedgerUnavailable,
    AiPricing,
    billing_period_start,
)
from app.modules.concierge.domain.intent import INTENT_VERSION, Intent
from app.modules.concierge.domain.recommendation import (
    RANKING_PROMPT_VERSION,
    RECOMMENDATION_VERSION,
    RankedGame,
    RankingPayload,
    RecommendationCandidateSnapshot,
    RecommendationOfferSnapshot,
    RecommendationPlan,
    RecommendationProvenance,
)
from app.modules.concierge.domain.session import MAX_OUTBOX_TEXT_CHARS
from app.modules.concierge.ports.ai_ledger import AiLedger
from app.modules.concierge.ports.models import ModelCallFailure, ModelGateway
from app.modules.concierge.ports.recommendations import RecommendationOutcome

_FALLBACK = (
    "Não consegui confirmar opções comerciais agora. Você pode pesquisar o "
    "catálogo ou falar com uma pessoa usando /humano."
)
_NO_MATCH = (
    "Ainda não encontrei uma opção publicada com os filtros comerciais que você "
    "informou. Você pode ajustar a busca no catálogo ou falar com uma pessoa usando /humano."
)
_NO_RANKED_MATCH = (
    "Não encontrei uma recomendação adequada entre as opções comerciais elegíveis. "
    "Você pode ajustar suas preferências, pesquisar o catálogo ou falar com uma pessoa usando /humano."
)
_UNVERIFIABLE_CONSTRAINT = (
    "O catálogo publicado não permite confirmar uma ou mais restrições informadas. "
    "Você pode ajustar a busca no catálogo ou falar com uma pessoa usando /humano."
)
_GREETING = "Oi! Sou Pixel, assistente de IA da RetroVault. Esta conversa de demonstração acontece no Sandbox."
_EDITORIAL_MATCH_FIELDS = ("title", "genre", "description", "developer", "publisher", "edition")
_RELEVANCE_REFS = {
    "intent:platform",
    "intent:genre",
    "intent:style_match",
    "intent:price",
    "intent:mode",
}


class RecommendationService:
    def __init__(
        self,
        gateway: ModelGateway | None,
        ledger: AiLedger | None,
        discovery: PublicDiscovery,
        *,
        pricing: AiPricing,
        model_snapshot: str,
        configuration_version: str,
        workflow_version: str,
        public_site_url: str,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.discovery = discovery
        self.pricing = pricing
        self.model_snapshot = model_snapshot
        self.configuration_version = configuration_version
        self.workflow_version = workflow_version
        self.public_site_url = public_site_url.rstrip("/")

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
    ) -> RecommendationOutcome:
        if self.public_site_url == "":
            return RecommendationOutcome("fallback", _FALLBACK)
        if intent.constraints:
            return RecommendationOutcome("empty", _UNVERIFIABLE_CONSTRAINT)
        has_actionable_filter = bool(
            intent.platform
            or intent.genre
            or (intent.style and len(intent.style.strip()) >= 2)
            or intent.mode
            or intent.price_min_brl_cents is not None
            or intent.price_max_brl_cents is not None
        )
        if not has_actionable_filter:
            if intent.players is not None:
                return RecommendationOutcome(
                    "empty",
                    "O catálogo não informa número de jogadores, então não consigo "
                    "confirmar essa compatibilidade. Você pode pesquisar o catálogo "
                    "ou falar com uma pessoa usando /humano.",
                )
            return RecommendationOutcome("empty", _NO_MATCH)
        previous_plan = RecommendationPlan.from_dict(previous_recommendation)
        discovery_exclusions = tuple(
            dict.fromkeys(
                (
                    *excluded_game_ids,
                    *(previous_plan.presented_game_ids if previous_plan else ()),
                )
            )
        )
        criteria = RecommendationCriteria(
            query=intent.style,
            platform=intent.platform,
            genre=intent.genre,
            mode=intent.mode,
            price_min_brl_cents=intent.price_min_brl_cents,
            price_max_brl_cents=intent.price_max_brl_cents,
            excluded_game_ids=discovery_exclusions,
        )
        try:
            candidates = self.discovery.recommend_candidates(criteria)
        except Exception:
            return RecommendationOutcome("fallback", _FALLBACK)
        if not candidates:
            reused = self._reuse_previous(
                previous_recommendation,
                intent,
                update_id=update_id,
                correlation_id=correlation_id,
            )
            if reused is not None:
                initial_text = self._compose(reused, self._snapshot_map(reused))
                if initial_text != _FALLBACK:
                    return RecommendationOutcome(
                        "accepted", initial_text, reused.to_dict()
                    )
            return RecommendationOutcome("empty", _NO_MATCH)
        if self.gateway is None or self.ledger is None or not self.pricing.enabled:
            return RecommendationOutcome("fallback", _FALLBACK)

        snapshots = tuple(
            self._snapshot(candidate, intent)
            for candidate in candidates[:20]
        )
        snapshots = tuple(
            candidate for candidate in snapshots
            if set(candidate.evidence_refs) & _RELEVANCE_REFS
        )
        if not snapshots:
            reused = self._reuse_previous(
                previous_recommendation,
                intent,
                update_id=update_id,
                correlation_id=correlation_id,
            )
            if reused is not None:
                initial_text = self._compose(reused, self._snapshot_map(reused))
                if initial_text != _FALLBACK:
                    return RecommendationOutcome(
                        "accepted", initial_text, reused.to_dict()
                    )
            return RecommendationOutcome("empty", _NO_MATCH)
        allowed_ids = {candidate.game_id for candidate in snapshots}
        model_candidates = [self._model_candidate(candidate) for candidate in snapshots]
        try:
            upper_bound = self.gateway.ranking_input_token_upper_bound(
                intent, model_candidates
            )
        except Exception:
            return RecommendationOutcome("fallback", _FALLBACK)
        if (
            type(upper_bound) is not int
            or upper_bound < 0
            or upper_bound > self.pricing.max_input_tokens
        ):
            return RecommendationOutcome("fallback", _FALLBACK)

        now = datetime.now(UTC)
        reserved_cost = self.pricing.maximum_cost()
        if reserved_cost <= 0 or reserved_cost > self.pricing.monthly_budget_usd:
            return RecommendationOutcome("fallback", _FALLBACK)
        try:
            grant = self.ledger.reserve(
                operation="recommendation_ranking",
                session_id=session_id,
                channel=channel,
                update_id=update_id,
                correlation_id=correlation_id,
                period_start=billing_period_start(now),
                model_snapshot=self.model_snapshot,
                prompt_version=RANKING_PROMPT_VERSION,
                workflow_version=self.workflow_version,
                configuration_version=self.configuration_version,
                input_token_limit=self.pricing.max_input_tokens,
                output_token_limit=self.pricing.max_output_tokens,
                reserved_cost_usd=reserved_cost,
                monthly_budget_usd=self.pricing.monthly_budget_usd,
                now=now,
                expires_at=now + timedelta(
                    seconds=self.pricing.reservation_ttl_seconds
                ),
            )
        except Exception:
            return RecommendationOutcome("fallback", _FALLBACK)
        if not grant.allowed or not grant.created:
            return RecommendationOutcome("fallback", _FALLBACK)

        try:
            result = self.gateway.rank_recommendations(
                intent, model_candidates, correlation_id=correlation_id
            )
        except ModelCallFailure as exc:
            if exc.conclusive:
                self._release(grant.reservation_id, now)
            return RecommendationOutcome("fallback", _FALLBACK)
        except Exception:
            # A timeout may follow provider acceptance; keep the reservation.
            return RecommendationOutcome("fallback", _FALLBACK)

        input_tokens = result.usage.input_tokens
        output_tokens = result.usage.output_tokens
        if input_tokens < 0 or output_tokens < 0:
            return RecommendationOutcome("fallback", _FALLBACK)
        try:
            reconciled = self.ledger.reconcile(
                grant.reservation_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                actual_cost_usd=self.pricing.cost(input_tokens, output_tokens),
                now=datetime.now(UTC),
            )
        except AiLedgerUnavailable:
            return RecommendationOutcome("fallback", _FALLBACK)
        if (
            not reconciled
            or input_tokens > self.pricing.max_input_tokens
            or output_tokens > self.pricing.max_output_tokens
        ):
            return RecommendationOutcome("fallback", _FALLBACK)
        try:
            ranking_payload = RankingPayload.from_json(result.output_json)
        except (TypeError, ValueError):
            return RecommendationOutcome("fallback", _FALLBACK)
        if not ranking_payload.recommendations:
            reused = self._reuse_previous(
                previous_recommendation,
                intent,
                update_id=update_id,
                correlation_id=correlation_id,
            )
            if reused is not None:
                initial_text = self._compose(reused, self._snapshot_map(reused))
                if initial_text != _FALLBACK:
                    return RecommendationOutcome(
                        "accepted", initial_text, reused.to_dict()
                    )
            return RecommendationOutcome("empty", _NO_RANKED_MATCH)
        by_id = {candidate.game_id: candidate for candidate in snapshots}
        for ranked in ranking_payload.recommendations:
            candidate = by_id.get(ranked.game_id)
            if (
                ranked.game_id not in allowed_ids
                or candidate is None
                or not set(ranked.evidence).issubset(candidate.evidence_refs)
                or not set(ranked.evidence) & _RELEVANCE_REFS
            ):
                # Invalid IDs or evidence invalidate the entire ranking.
                return RecommendationOutcome("fallback", _FALLBACK)

        plan = RecommendationPlan(
            provenance=RecommendationProvenance(
                recommendation_version=RECOMMENDATION_VERSION,
                ranking_prompt_version=RANKING_PROMPT_VERSION,
                intent_version=INTENT_VERSION,
                workflow_version=self.workflow_version,
                configuration_version=self.configuration_version,
                model_snapshot=self.model_snapshot,
                source_update_id=update_id,
                correlation_id=correlation_id,
            ),
            intent=intent,
            candidates=snapshots,
            ranking=ranking_payload.recommendations,
        )
        initial_text = self._compose(plan, self._snapshot_map(plan))
        if initial_text == _FALLBACK:
            return RecommendationOutcome("fallback", _FALLBACK)
        return RecommendationOutcome("accepted", initial_text, plan.to_dict())

    def revalidate_and_compose(self, context: object) -> str:
        plan = RecommendationPlan.from_dict(context)
        if plan is None:
            return _FALLBACK
        checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        revalidated_facts: list[dict[str, object]] = []
        try:
            fresh: dict[UUID, tuple[PublishedGame, tuple[Offer, ...]]] = {}
            candidates = {candidate.game_id: candidate for candidate in plan.candidates}
            for ranked in plan.ranking:
                saved = candidates.get(ranked.game_id)
                if saved is None:
                    return self._with_greeting(plan, _FALLBACK)
                game = self.discovery.catalog.get_game(ranked.game_id)
                if game is None:
                    revalidated_facts.append(
                        {"game_id": str(ranked.game_id), "status": "unpublished"}
                    )
                    continue
                if (
                    game.version != saved.version
                    or game.title != saved.title
                    or game.platform != saved.platform
                    or self._genre(game) != saved.genre
                ):
                    revalidated_facts.append(
                        {"game_id": str(ranked.game_id), "status": "catalog_changed"}
                    )
                    continue
                if (
                    plan.intent.platform
                    and not self._same(plan.intent.platform, game.platform)
                ):
                    revalidated_facts.append(
                        {"game_id": str(ranked.game_id), "status": "catalog_changed"}
                    )
                    continue
                current_genre = self._genre(game)
                if (
                    plan.intent.genre
                    and (
                        current_genre is None
                        or not self._same(plan.intent.genre, current_genre)
                    )
                ):
                    revalidated_facts.append(
                        {"game_id": str(ranked.game_id), "status": "catalog_changed"}
                    )
                    continue
                current_offers = self.discovery.get_game_offers(ranked.game_id)
                saved_offer_ids = {offer.offer_id for offer in saved.offers}
                offers = tuple(
                    sorted(
                        (
                            offer for offer in current_offers
                            if offer.id in saved_offer_ids
                            and self._offer_matches(offer, plan.intent)
                        ),
                        key=lambda offer: (
                            offer.demo_rank, offer.price_minor, offer.mode, str(offer.id)
                        ),
                    )
                )
                if offers:
                    fresh[ranked.game_id] = (game, offers)
                    revalidated_facts.append(
                        {
                            "game_id": str(ranked.game_id),
                            "status": "eligible",
                            "title": game.title,
                            "platform": game.platform,
                            "genre": self._genre(game),
                            "version": game.version,
                            "offers": [self._offer_snapshot(offer).to_dict() for offer in offers],
                        }
                    )
                else:
                    revalidated_facts.append(
                        {"game_id": str(ranked.game_id), "status": "no_eligible_offer"}
                    )
            composed = self._compose(plan, fresh)
            reply_text = self._with_greeting(plan, composed)
            presented_game_ids = (
                [
                    str(item.game_id)
                    for item in plan.ranking
                    if item.game_id in fresh
                ][:3]
                if composed != _FALLBACK and reply_text != _FALLBACK
                else []
            )
            self._save_revalidation(
                context, checked_at, revalidated_facts, presented_game_ids
            )
            return reply_text
        except Exception:
            if not revalidated_facts and plan.ranking:
                revalidated_facts.append(
                    {
                        "game_id": str(plan.ranking[0].game_id),
                        "status": "revalidation_unavailable",
                    }
                )
            self._save_revalidation(context, checked_at, revalidated_facts, [])
            return self._with_greeting(plan, _FALLBACK)

    @staticmethod
    def _offer_snapshot(offer: Offer) -> RecommendationOfferSnapshot:
        return RecommendationOfferSnapshot(
            offer_id=offer.id,
            mode=offer.mode,
            price_minor=offer.price_minor,
            currency=offer.currency,
            condition_summary=offer.condition_summary,
            available_units=offer.available_units,
            sandbox=offer.sandbox,
        )

    def _reuse_previous(
        self,
        previous_recommendation: object,
        intent: Intent,
        *,
        update_id: int,
        correlation_id: UUID,
    ) -> RecommendationPlan | None:
        previous = RecommendationPlan.from_dict(previous_recommendation)
        if previous is None or not previous.presented_game_ids:
            return None
        if intent.style and not self._same(intent.style, previous.intent.style or ""):
            return None
        candidates = {item.game_id: item for item in previous.candidates}
        presented_ids = set(previous.presented_game_ids)
        reused_candidates: list[RecommendationCandidateSnapshot] = []
        reused_ranking: list[RankedGame] = []
        for ranked in previous.ranking:
            if ranked.game_id not in presented_ids:
                continue
            candidate = candidates.get(ranked.game_id)
            if candidate is None:
                continue
            if intent.platform and not self._same(intent.platform, candidate.platform):
                continue
            if intent.genre and (
                candidate.genre is None
                or not self._same(intent.genre, candidate.genre)
            ):
                continue
            offers = tuple(
                offer
                for offer in candidate.offers
                if self._offer_snapshot_matches(offer, intent)
            )
            if not offers:
                continue
            reused_candidates.append(replace(candidate, offers=offers))
            reused_ranking.append(ranked)
        if not reused_candidates:
            return None
        provenance = RecommendationProvenance(
            recommendation_version=RECOMMENDATION_VERSION,
            ranking_prompt_version=RANKING_PROMPT_VERSION,
            intent_version=INTENT_VERSION,
            workflow_version=self.workflow_version,
            configuration_version=self.configuration_version,
            model_snapshot=self.model_snapshot,
            source_update_id=update_id,
            correlation_id=correlation_id,
        )
        return RecommendationPlan(
            provenance=provenance,
            intent=intent,
            candidates=tuple(reused_candidates),
            ranking=tuple(reused_ranking),
            greeting_required=previous.greeting_required,
            reused_previous_options=True,
        )

    @staticmethod
    def _offer_snapshot_matches(offer: RecommendationOfferSnapshot, intent: Intent) -> bool:
        return bool(
            offer.available_units > 0
            and (intent.mode is None or offer.mode == intent.mode)
            and (
                intent.price_min_brl_cents is None
                or offer.price_minor >= intent.price_min_brl_cents
            )
            and (
                intent.price_max_brl_cents is None
                or offer.price_minor <= intent.price_max_brl_cents
            )
        )

    @staticmethod
    def _save_revalidation(
        context: object,
        checked_at: str,
        facts: list[dict[str, object]],
        presented_game_ids: list[str],
    ) -> None:
        if type(context) is dict:
            raw = cast(dict[str, object], context)
            if facts:
                raw["revalidation"] = {"checked_at": checked_at, "facts": facts[:3]}
            raw["presented_game_ids"] = presented_game_ids

    def _snapshot(
        self,
        candidate: DiscoveryCandidate,
        intent: Intent,
    ) -> RecommendationCandidateSnapshot:
        game = candidate.game
        genre = self._genre(game)
        refs: list[str] = []
        if intent.platform and self._same(intent.platform, game.platform):
            refs.append("intent:platform")
        if intent.genre and genre and self._same(intent.genre, genre):
            refs.append("intent:genre")
        if candidate.matched_fields:
            refs.append("intent:style_match")
        if intent.mode is not None:
            refs.append("intent:mode")
        if intent.price_min_brl_cents is not None or intent.price_max_brl_cents is not None:
            refs.append("intent:price")
        refs.extend(f"commerce:offer:{offer.id}" for offer in candidate.offers)
        matched_fields = tuple(
            field for field in _EDITORIAL_MATCH_FIELDS if field in candidate.matched_fields
        )
        matched_attributes = self._matched_attributes(game, genre, matched_fields)
        offers = tuple(
            RecommendationOfferSnapshot(
                offer_id=offer.id,
                mode=offer.mode,
                price_minor=offer.price_minor,
                currency=offer.currency,
                condition_summary=offer.condition_summary,
                available_units=offer.available_units,
                sandbox=offer.sandbox,
            )
            for offer in candidate.offers
        )
        return RecommendationCandidateSnapshot(
            game_id=game.id,
            title=game.title,
            platform=game.platform,
            genre=genre,
            version=game.version,
            offers=offers,
            evidence_refs=tuple(refs),
            matched_fields=matched_fields,
            matched_attributes=matched_attributes,
        )

    @staticmethod
    def _model_candidate(candidate: RecommendationCandidateSnapshot) -> dict[str, object]:
        return {
            "game_id": str(candidate.game_id),
            "title": RecommendationService._display_catalog_text(candidate.title, 200),
            "platform": RecommendationService._display_catalog_text(candidate.platform, 96),
            "genre": (
                RecommendationService._display_catalog_text(candidate.genre, 96)
                if candidate.genre is not None else None
            ),
            "offers": [
                {
                    "offer_id": str(offer.offer_id),
                    "mode": offer.mode,
                    "price_minor": offer.price_minor,
                    "currency": offer.currency,
                    "available_units": offer.available_units,
                    "sandbox": offer.sandbox,
                }
                for offer in candidate.offers
            ],
            "evidence_refs": list(candidate.evidence_refs),
            "matched_fields": list(candidate.matched_fields),
            "matched_attributes": dict(candidate.matched_attributes),
        }

    @staticmethod
    def _matched_attributes(
        game: PublishedGame,
        genre: str | None,
        matched_fields: tuple[str, ...],
    ) -> tuple[tuple[str, str], ...]:
        attributes = game.editorial.get("attributes")
        values = attributes if isinstance(attributes, dict) else {}
        result: list[tuple[str, str]] = []
        for field in matched_fields:
            if field == "title":
                value = game.title
            elif field == "genre":
                value = genre
            else:
                value = values.get(field)
            if isinstance(value, str):
                normalized = RecommendationService._display_catalog_text(value, 240)
                if normalized:
                    result.append((field, normalized))
        return tuple(result)

    @staticmethod
    def _snapshot_map(
        plan: RecommendationPlan,
    ) -> dict[UUID, tuple[PublishedGame, tuple[Offer, ...]]]:
        # Initial rendering uses just-read candidate facts. It is replaced by
        # a fresh validation pass before every actual send.
        result: dict[UUID, tuple[PublishedGame, tuple[Offer, ...]]] = {}
        for candidate in plan.candidates:
            game = PublishedGame(
                id=candidate.game_id,
                title=candidate.title,
                platform=candidate.platform,
                editorial={"attributes": {"genre": candidate.genre}},
                source="",
                source_record_id="",
                version=candidate.version,
                etag="",
                verified_at=datetime.now(UTC),
                cover_hash="",
                cover_content_type="",
                cover_attribution="",
            )
            offers = tuple(
                Offer(
                    id=offer.offer_id,
                    game_id=candidate.game_id,
                    mode=offer.mode,
                    price_minor=offer.price_minor,
                    currency=offer.currency,
                    condition_summary=offer.condition_summary,
                    available_units=offer.available_units,
                    demo_rank=index,
                    sandbox=offer.sandbox,
                )
                for index, offer in enumerate(candidate.offers)
            )
            result[candidate.game_id] = (game, offers)
        return result

    def _compose(
        self,
        plan: RecommendationPlan,
        fresh: dict[UUID, tuple[PublishedGame, tuple[Offer, ...]]],
    ) -> str:
        ranked = [item for item in plan.ranking if item.game_id in fresh][:3]
        if not ranked or not self.public_site_url:
            return _FALLBACK
        candidate_by_id = {candidate.game_id: candidate for candidate in plan.candidates}
        if plan.reused_previous_options:
            lines = [
                "Não encontrei outra opção elegível com os critérios atuais sem repetir opções já vistas. "
                "Reapresento as anteriores com as ofertas revalidadas agora:"
            ]
        else:
            lines = ["Encontrei estas opções no catálogo que podem combinar com seu pedido:"]
        for index, ranked_game in enumerate(ranked, 1):
            game, offers = fresh[ranked_game.game_id]
            candidate = candidate_by_id[ranked_game.game_id]
            reason = self._reason(game, offers, candidate, ranked_game, plan.intent)
            title = self._display_catalog_text(game.title, 200) or "Título indisponível"
            platform = self._display_catalog_text(game.platform, 96) or "plataforma não informada"
            lines.append(f"{index}. {title} ({platform}) — {reason}")
            for offer in self._display_offers(offers):
                label = "Compra" if offer.mode == "purchase" else "Aluguel"
                price = self._format_brl(offer.price_minor)
                unit_label = "unidade" if offer.available_units == 1 else "unidades"
                sandbox = " (Sandbox de demonstração)" if offer.sandbox else ""
                lines.append(
                    f"   {label}: {price}; condição: {self._display_condition(offer.condition_summary)}; "
                    f"{offer.available_units} {unit_label}{sandbox}."
                )
            lines.append(f"   {self.public_site_url}/games/{game.id}")
        if plan.intent.players is not None:
            lines.append(
                "O catálogo não informa número de jogadores, então não consigo "
                "confirmar essa compatibilidade."
            )
        text = "\n".join(lines)
        return text if len(text) <= MAX_OUTBOX_TEXT_CHARS else _FALLBACK

    @staticmethod
    def _with_greeting(plan: RecommendationPlan, text: str) -> str:
        if not plan.greeting_required or text.startswith(_GREETING):
            result = text
        else:
            result = f"{_GREETING} {text}"
        return result if len(result) <= MAX_OUTBOX_TEXT_CHARS else _FALLBACK

    @staticmethod
    def _display_catalog_text(value: str, maximum: int) -> str:
        printable = "".join(char if char.isprintable() else " " for char in value)
        normalized = " ".join(printable.split())
        if len(normalized) > maximum:
            return normalized[: maximum - 3].rstrip() + "..."
        return normalized

    @staticmethod
    def _display_condition(value: str) -> str:
        return RecommendationService._display_catalog_text(value, 160)

    @staticmethod
    def _reason(
        game: PublishedGame,
        offers: tuple[Offer, ...],
        candidate: RecommendationCandidateSnapshot,
        ranked: RankedGame,
        intent: Intent,
    ) -> str:
        evidence = set(ranked.evidence)
        reasons: list[str] = []
        genre_value = RecommendationService._genre(game)
        genre = (
            RecommendationService._display_catalog_text(genre_value, 96)
            if genre_value is not None else None
        )
        platform = RecommendationService._display_catalog_text(game.platform, 96)
        if "intent:genre" in evidence and genre:
            reasons.append(f"o catálogo o classifica como {genre}")
        if "intent:platform" in evidence:
            reasons.append(f"está cadastrado para {platform}")
        if "intent:style_match" in evidence:
            reasons.append("os dados publicados coincidem com a busca textual pelo estilo descrito")
        if "intent:mode" in evidence:
            modes = sorted({offer.mode for offer in offers})
            labels = ["compra" if mode == "purchase" else "aluguel" for mode in modes]
            reasons.append("há oferta de " + " e ".join(labels))
        if "intent:price" in evidence:
            reasons.append("a oferta está na faixa de preço informada")
        if not reasons:
            return "há oferta comercial verificada no catálogo"
        return "; ".join(reasons[:2]) + "."

    @staticmethod
    def _display_offers(offers: tuple[Offer, ...]) -> tuple[Offer, ...]:
        best_by_mode: dict[str, Offer] = {}
        for offer in offers:
            current = best_by_mode.get(offer.mode)
            if current is None or (offer.price_minor, offer.demo_rank) < (
                current.price_minor, current.demo_rank
            ):
                best_by_mode[offer.mode] = offer
        return tuple(best_by_mode[key] for key in sorted(best_by_mode))

    @staticmethod
    def _format_brl(amount: int) -> str:
        reais, cents = divmod(amount, 100)
        return f"R$ {reais:,}".replace(",", ".") + f",{cents:02d}"

    @staticmethod
    def _genre(game: PublishedGame) -> str | None:
        attributes = game.editorial.get("attributes")
        if not isinstance(attributes, dict):
            return None
        value = attributes.get("genre")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _same(left: str, right: str) -> bool:
        return " ".join(left.casefold().split()) == " ".join(right.casefold().split())

    @staticmethod
    def _offer_matches(offer: Offer, intent: Intent) -> bool:
        return bool(
            offer.available_units > 0
            and (intent.mode is None or offer.mode == intent.mode)
            and (
                intent.price_min_brl_cents is None
                or offer.price_minor >= intent.price_min_brl_cents
            )
            and (
                intent.price_max_brl_cents is None
                or offer.price_minor <= intent.price_max_brl_cents
            )
        )

    def _release(self, reservation_id: UUID, now: datetime) -> None:
        try:
            if self.ledger is not None:
                self.ledger.release(reservation_id, now=now)
        except AiLedgerUnavailable:
            return
