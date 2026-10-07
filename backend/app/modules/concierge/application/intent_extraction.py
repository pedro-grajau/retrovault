"""Budgeted, strict intent extraction with deterministic safe fallbacks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from app.modules.concierge.domain.ai_ledger import (
    AiLedgerUnavailable,
    AiPricing,
    billing_period_start,
)
from app.modules.concierge.domain.intent import (
    INTENT_VERSION,
    PROMPT_VERSION,
    Intent,
    IntentExtractionPayload,
    IntentProvenance,
    is_prompt_injection,
    merge_intent,
)
from app.modules.concierge.ports.ai_ledger import AiLedger
from app.modules.concierge.ports.models import ModelCallFailure, ModelGateway

ExtractionStatus = Literal["accepted", "fallback", "injection"]
SafetyClassification = Literal["normal", "suspected_injection"]


@dataclass(frozen=True)
class IntentDecision:
    status: ExtractionStatus
    intent: Intent
    clarification_field: str
    provenance: IntentProvenance


class IntentExtractionService:
    def __init__(
        self,
        gateway: ModelGateway | None,
        ledger: AiLedger | None,
        *,
        pricing: AiPricing,
        model_snapshot: str,
        configuration_version: str,
        workflow_version: str,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.pricing = pricing
        self.model_snapshot = model_snapshot
        self.configuration_version = configuration_version
        self.workflow_version = workflow_version

    def extract(
        self,
        message_text: str,
        previous_intent: Intent | None,
        *,
        session_id: UUID,
        channel: str,
        update_id: int,
        correlation_id: UUID,
        now: datetime | None = None,
    ) -> IntentDecision:
        current = now or datetime.now(UTC)
        prior = previous_intent or Intent()
        safety: SafetyClassification = (
            "suspected_injection" if is_prompt_injection(message_text) else "normal"
        )
        if safety == "suspected_injection":
            return self._decision(
                "injection", prior, "none", update_id, correlation_id, safety
            )
        if (
            self.gateway is None
            or self.ledger is None
            or not self.pricing.enabled
            or not self.model_snapshot
        ):
            return self._fallback(prior, update_id, correlation_id, safety)

        try:
            input_upper_bound = self.gateway.input_token_upper_bound(
                message_text, prior
            )
        except Exception:
            return self._fallback(prior, update_id, correlation_id, safety)
        if (
            type(input_upper_bound) is not int
            or input_upper_bound < 0
            or input_upper_bound > self.pricing.max_input_tokens
        ):
            return self._fallback(prior, update_id, correlation_id, safety)

        reserved_cost = self.pricing.maximum_cost()
        if reserved_cost <= 0 or reserved_cost > self.pricing.monthly_budget_usd:
            return self._fallback(prior, update_id, correlation_id, safety)
        try:
            grant = self.ledger.reserve(
                session_id=session_id,
                channel=channel,
                update_id=update_id,
                correlation_id=correlation_id,
                period_start=billing_period_start(current),
                model_snapshot=self.model_snapshot,
                prompt_version=PROMPT_VERSION,
                workflow_version=self.workflow_version,
                configuration_version=self.configuration_version,
                input_token_limit=self.pricing.max_input_tokens,
                output_token_limit=self.pricing.max_output_tokens,
                reserved_cost_usd=reserved_cost,
                monthly_budget_usd=self.pricing.monthly_budget_usd,
                now=current,
                expires_at=current + timedelta(
                    seconds=self.pricing.reservation_ttl_seconds
                ),
            )
        except AiLedgerUnavailable:
            return self._fallback(prior, update_id, correlation_id, safety)
        if not grant.allowed or not grant.created:
            return self._fallback(prior, update_id, correlation_id, safety)

        try:
            result = self.gateway.extract_intent(
                message_text, prior, correlation_id=correlation_id
            )
        except ModelCallFailure as exc:
            if exc.conclusive:
                self._release(grant.reservation_id, current)
            return self._fallback(prior, update_id, correlation_id, safety)
        except Exception:
            # Connection loss or an unexpected SDK error may follow provider
            # acceptance. Keep the reservation until the orphan policy expires it.
            return self._fallback(prior, update_id, correlation_id, safety)

        input_tokens = result.usage.input_tokens
        output_tokens = result.usage.output_tokens
        if input_tokens < 0 or output_tokens < 0:
            return self._fallback(prior, update_id, correlation_id, safety)
        actual_cost = self.pricing.cost(input_tokens, output_tokens)
        try:
            reconciled = self.ledger.reconcile(
                grant.reservation_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                actual_cost_usd=actual_cost,
                now=datetime.now(UTC),
            )
        except AiLedgerUnavailable:
            return self._fallback(prior, update_id, correlation_id, safety)
        if not reconciled:
            return self._fallback(prior, update_id, correlation_id, safety)
        if (
            input_tokens > self.pricing.max_input_tokens
            or output_tokens > self.pricing.max_output_tokens
        ):
            return self._fallback(prior, update_id, correlation_id, safety)

        try:
            extracted = IntentExtractionPayload.model_validate_json(result.output_json)
        except (ValidationError, ValueError, TypeError):
            return self._fallback(prior, update_id, correlation_id, safety)
        merged = merge_intent(prior, extracted)
        return self._decision(
            "accepted",
            merged,
            extracted.clarification_field,
            update_id,
            correlation_id,
            safety,
        )

    def _release(self, reservation_id: UUID, now: datetime) -> None:
        ledger = self.ledger
        if ledger is None:
            return
        try:
            ledger.release(reservation_id, now=now)
        except AiLedgerUnavailable:
            return

    def _fallback(
        self,
        prior: Intent,
        update_id: int,
        correlation_id: UUID,
        safety: SafetyClassification,
    ) -> IntentDecision:
        return self._decision(
            "fallback", prior, "none", update_id, correlation_id, safety
        )

    def _decision(
        self,
        status: ExtractionStatus,
        intent: Intent,
        clarification_field: str,
        update_id: int,
        correlation_id: UUID,
        safety: SafetyClassification,
    ) -> IntentDecision:
        provenance = IntentProvenance(
            intent_version=INTENT_VERSION,
            prompt_version=PROMPT_VERSION,
            workflow_version=self.workflow_version,
            configuration_version=self.configuration_version,
            source_update_id=update_id,
            correlation_id=correlation_id,
            safety_classification=safety,
        )
        return IntentDecision(status, intent, clarification_field, provenance)
