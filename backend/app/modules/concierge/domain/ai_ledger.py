"""Budget records and policy for paid model calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

LEDGER_TIMEZONE = ZoneInfo("America/Fortaleza")
MONTHLY_BUDGET_USD = Decimal("25.00")
AiReservationStatus = Literal["reserved", "posted", "released", "expired"]


class AiLedgerUnavailable(RuntimeError):
    """The authoritative PostgreSQL budget ledger is unavailable."""


@dataclass(frozen=True)
class AiPricing:
    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal
    max_input_tokens: int
    max_output_tokens: int
    reservation_ttl_seconds: int = 900
    monthly_budget_usd: Decimal = MONTHLY_BUDGET_USD

    @property
    def enabled(self) -> bool:
        return (
            self.input_usd_per_million_tokens > 0
            and self.output_usd_per_million_tokens > 0
            and self.max_input_tokens > 0
            and self.max_output_tokens > 0
            and self.monthly_budget_usd > 0
            and self.monthly_budget_usd <= MONTHLY_BUDGET_USD
        )

    def maximum_cost(self) -> Decimal:
        return price_tokens(
            self.max_input_tokens,
            self.max_output_tokens,
            self.input_usd_per_million_tokens,
            self.output_usd_per_million_tokens,
        )

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return price_tokens(
            input_tokens,
            output_tokens,
            self.input_usd_per_million_tokens,
            self.output_usd_per_million_tokens,
        )


def price_tokens(
    input_tokens: int,
    output_tokens: int,
    input_rate: Decimal,
    output_rate: Decimal,
) -> Decimal:
    raw = (
        Decimal(input_tokens) * input_rate
        + Decimal(output_tokens) * output_rate
    ) / Decimal(1_000_000)
    return raw.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)


def billing_period_start(now: datetime) -> date:
    local = now.astimezone(LEDGER_TIMEZONE)
    return date(local.year, local.month, 1)


@dataclass(frozen=True)
class ReservationGrant:
    reservation_id: UUID
    status: AiReservationStatus
    created: bool
    allowed: bool
